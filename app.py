import os
import uuid
import threading
import time
import base64
import subprocess

from flask import Flask, request, jsonify, send_from_directory, render_template
import yt_dlp
import cv2

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# How long a downloaded file is kept before being cleaned up (seconds)
FILE_TTL = 60 * 30  # 30 minutes


def cleanup_old_files():
    """Background loop that deletes downloaded files older than FILE_TTL."""
    while True:
        now = time.time()
        for fname in os.listdir(DOWNLOAD_DIR):
            fpath = os.path.join(DOWNLOAD_DIR, fname)
            try:
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > FILE_TTL:
                    os.remove(fpath)
            except OSError:
                pass
        time.sleep(60)


threading.Thread(target=cleanup_old_files, daemon=True).start()


def base_ydl_opts():
    """Shared yt-dlp options."""
    return {
        "quiet": True,
        "no_warnings": True,
    }



@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    """Given a TikTok URL, return title/thumbnail/duration without downloading yet."""
    data = request.get_json(force=True)
    url = (data or {}).get("url", "").strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    ydl_opts = base_ydl_opts()
    ydl_opts["skip_download"] = True

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": f"Could not read video info: {str(e)}"}), 400

    return jsonify({
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
    })


def download_source_video(url):
    """Download a video from a URL and return (filepath, info dict).
    Raises an exception on failure."""
    file_id = str(uuid.uuid4())
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    ydl_opts = base_ydl_opts()
    ydl_opts.update({
        "format": "bestvideo+bestaudio/best",
        "outtmpl": out_template,
        "merge_output_format": "mp4",
    })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            base, _ = os.path.splitext(filename)
            filename = base + ".mp4"

    if not os.path.exists(filename):
        raise RuntimeError("Downloaded file not found on disk")

    return filename, info


def extract_sample_frames(video_path, num_frames=10):
    """Grab `num_frames` evenly-spaced JPEG frames from the video.
    Returns a list of (timestamp_seconds, base64_jpeg_bytes)."""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total_frames / fps if fps else 0

    samples = []
    if total_frames <= 0:
        cap.release()
        return samples, duration

    for i in range(num_frames):
        frame_idx = int((i + 0.5) * total_frames / num_frames)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok:
            continue
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            continue
        timestamp = frame_idx / fps if fps else 0
        samples.append((timestamp, base64.b64encode(buf.tobytes()).decode("ascii")))

    cap.release()
    return samples, duration


def detect_cart_moment(video_path):
    """Sample frames from the video and ask Gemini which one shows a product
    being placed into a shopping cart. Returns a timestamp (seconds), or None
    if nothing was confidently detected."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured on the server")

    import google.generativeai as genai

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    samples, duration = extract_sample_frames(video_path, num_frames=10)
    if not samples:
        return None, duration

    parts = ["Here are frames sampled evenly across a shopping-haul video, "
             "in order, each labeled with its timestamp in seconds. "
             "Reply with ONLY the timestamp (a number) of the single frame "
             "that best shows a person placing a product into a shopping "
             "cart or basket. If none show this clearly, reply with 'none'."]
    for ts, b64 in samples:
        parts.append(f"Timestamp: {ts:.1f}s")
        parts.append({"mime_type": "image/jpeg", "data": b64})

    response = model.generate_content(parts)
    text = (response.text or "").strip().lower()

    if "none" in text:
        return None, duration

    # Pull the first number out of the reply
    import re
    match = re.search(r"[\d.]+", text)
    if not match:
        return None, duration

    return float(match.group()), duration


def trim_clip(video_path, center_timestamp, clip_length=7, pre_roll=1):
    """Cut a clip_length-second clip out of video_path, positioned so that
    center_timestamp lands pre_roll seconds into the clip. Returns the output path."""
    start = max(0, center_timestamp - pre_roll)
    out_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}_clip.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", video_path,
        "-t", str(clip_length),
        "-c", "copy",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(out_path):
        # Fallback: re-encode instead of stream-copy (copy can fail to cut cleanly)
        cmd[cmd.index("-c") + 1] = "libx264"  # replace 'copy' with re-encode
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(f"ffmpeg trim failed: {result.stderr[-500:]}")

    return out_path


@app.route("/api/auto-clip", methods=["POST"])
def auto_clip():
    """Given a video URL, download it, detect the 'cart' moment, trim a clip
    around it, and return both the suggested timestamp and a preview link --
    so the result can be confirmed/adjusted before final export."""
    data = request.get_json(force=True)
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        video_path, info = download_source_video(url)
    except Exception as e:
        return jsonify({"error": f"Download failed: {str(e)}"}), 400

    try:
        timestamp, duration = detect_cart_moment(video_path)
    except Exception as e:
        return jsonify({"error": f"Detection failed: {str(e)}"}), 400

    if timestamp is None:
        return jsonify({
            "error": "Could not confidently detect a cart moment in this video.",
            "duration": duration,
            "source_file": os.path.basename(video_path),
        }), 200

    try:
        clip_path = trim_clip(video_path, timestamp)
    except Exception as e:
        return jsonify({"error": f"Trim failed: {str(e)}"}), 400

    return jsonify({
        "detected_timestamp": timestamp,
        "duration": duration,
        "clip_url": f"/api/file/{os.path.basename(clip_path)}",
        "source_file": os.path.basename(video_path),
        "title": info.get("title"),
    })


@app.route("/api/re-clip", methods=["POST"])
def re_clip():
    """Re-cut a clip from an already-downloaded source file at a
    user-adjusted timestamp (used when the auto-detected moment was wrong)."""
    data = request.get_json(force=True)
    source_file = (data or {}).get("source_file", "").strip()
    timestamp = float((data or {}).get("timestamp", 0))

    source_path = os.path.join(DOWNLOAD_DIR, source_file)
    if not source_file or not os.path.exists(source_path):
        return jsonify({"error": "Source video not found (it may have expired -- try again)"}), 400

    try:
        clip_path = trim_clip(source_path, timestamp)
    except Exception as e:
        return jsonify({"error": f"Trim failed: {str(e)}"}), 400

    return jsonify({"clip_url": f"/api/file/{os.path.basename(clip_path)}"})


@app.route("/api/download", methods=["POST"])
def download_video():
    """Given a URL, download the best-quality version and return a link
    the frontend can use to fetch the file."""
    data = request.get_json(force=True)
    url = (data or {}).get("url", "").strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        filename, info = download_source_video(url)
    except Exception as e:
        return jsonify({"error": f"Download failed: {str(e)}"}), 400

    basename = os.path.basename(filename)
    return jsonify({
        "download_url": f"/api/file/{basename}",
        "title": info.get("title"),
    })


@app.route("/api/file/<path:filename>")
def serve_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
