# TikTok HD Downloader

A simple web app: paste a TikTok link, get back the HD, no-watermark video file.

## How it works

- Frontend (`templates/index.html`) — single page, paste a link, hit Fetch to preview, then Download.
- Backend (`app.py`) — Flask server with two endpoints:
  - `POST /api/info` — pulls title/thumbnail/uploader without downloading (fast preview)
  - `POST /api/download` — uses `yt-dlp` to actually download the best-quality version to a temp folder, then returns a link to fetch it
- Downloaded files are auto-deleted after 30 minutes (background cleanup thread) so disk usage doesn't grow unbounded.

## Setup

```bash
cd tiktok-downloader
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Then open http://127.0.0.1:5000 in your browser, paste a TikTok link, and go.

## Notes / things you'll likely want to tweak

- **Rate limiting / abuse protection**: right now anyone who can reach the server can trigger downloads. If you deploy this publicly, add rate limiting (e.g. `flask-limiter`) or you'll get hammered.
- **yt-dlp updates**: TikTok changes its site frequently, which sometimes breaks extractors. Keep `yt-dlp` updated (`pip install -U yt-dlp`) since fixes ship often.
- **Storage**: downloaded files sit in `downloads/` temporarily. For production, consider streaming directly instead of writing to disk, or offloading to S3 with a signed URL.
- **Legal**: downloading and redistributing TikTok content may conflict with TikTok's Terms of Service and creators' copyright. This is intended for personal-use tooling — if you're building something public-facing/commercial, it's worth reviewing those implications.
