import os
import uuid
import threading
import time

from flask import Flask, request, jsonify, send_from_directory, render_template
import yt_dlp

app = Flask(__name__)

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
        # Retry once without cookies, in case the cookie read itself was the problem
        if "cookiesfrombrowser" in ydl_opts:
            try:
                fallback_opts = {k: v for k, v in ydl_opts.items() if k != "cookiesfrombrowser"}
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e2:
                return jsonify({"error": f"Could not read video info: {str(e2)}"}), 400
        else:
            return jsonify({"error": f"Could not read video info: {str(e)}"}), 400

    return jsonify({
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
    })


@app.route("/api/download", methods=["POST"])
def download_video():
    """Given a TikTok URL, download the best-quality (HD, no-watermark) version
    and return a link the frontend can use to fetch the file."""
    data = request.get_json(force=True)
    url = (data or {}).get("url", "").strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    file_id = str(uuid.uuid4())
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    ydl_opts = base_ydl_opts()
    ydl_opts.update({
        # Best video+audio combined; yt-dlp's TikTok extractor pulls the
        # non-watermarked source when available.
        "format": "bestvideo+bestaudio/best",
        "outtmpl": out_template,
        "merge_output_format": "mp4",
    })

    def do_download(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                base, _ = os.path.splitext(filename)
                filename = base + ".mp4"
            return info, filename

    try:
        info, filename = do_download(ydl_opts)
    except Exception as e:
        if "cookiesfrombrowser" in ydl_opts:
            try:
                fallback_opts = {k: v for k, v in ydl_opts.items() if k != "cookiesfrombrowser"}
                info, filename = do_download(fallback_opts)
            except Exception as e2:
                return jsonify({"error": f"Download failed: {str(e2)}"}), 400
        else:
            return jsonify({"error": f"Download failed: {str(e)}"}), 400

    if not os.path.exists(filename):
        return jsonify({"error": "Download failed: output file not found"}), 500

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
