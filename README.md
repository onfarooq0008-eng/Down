# ReelGrab

Web UI + REST API to download Instagram, Facebook and TikTok videos/reels (FastAPI + yt-dlp).

## Run
    pip install -r requirements.txt
    uvicorn app:app --port 8000        # needs ffmpeg installed for HD merge + MP3
    # or: docker build -t reelgrab . && docker run -p 8000:8000 reelgrab

Open http://localhost:8000 (UI) or http://localhost:8000/docs (API docs).

## API
    curl -X POST localhost:8000/api/info -H "Content-Type: application/json" \
         -d '{"url":"https://www.tiktok.com/@user/video/123"}'
    curl -OJ "localhost:8000/api/download?url=URL&type=video&quality=720"
    curl -OJ "localhost:8000/api/download?url=URL&type=audio" -H "X-API-Key: KEY"

## Environment variables
| Var | Default | Purpose |
|---|---|---|
| API_KEYS | (empty) | Comma-separated keys. Empty = open access |
| RATE_LIMIT_PER_MIN | 10 | Requests per minute per key/IP |
| MAX_CONCURRENT | 3 | Simultaneous downloads |
| MAX_FILE_MB | 500 | Max file size |
| COOKIES_FILE | (empty) | Netscape cookies.txt for login-gated Instagram/Facebook content |

## Notes
- Keep yt-dlp updated (`pip install -U yt-dlp`); these platforms change often.
- Public posts usually work; private or login-gated ones need COOKIES_FILE.
- Set API_KEYS before exposing this publicly.
- Download only content you own or have permission to save; respect platform terms and copyright.

## Deploy

### 1. Koyeb (all-in-one: site + API on one server)
1. Push this folder to a GitHub repo.
2. Koyeb > Create Service > GitHub > pick the repo > Builder: **Dockerfile**.
3. Exposed port **8000** (HTTP), health check path `/api/health`.
4. Env vars: `API_KEYS=yourkey` (recommended), optionally `RATE_LIMIT_PER_MIN`, `MAX_FILE_MB`, `COOKIES_FILE`.
5. Open the Koyeb URL: the website and `/docs` are both served from it.

### 2. Vercel (static website only, API stays on Koyeb)
1. Edit `web/config.js`: set `window.RG_BASE = "https://your-app.koyeb.app"`.
2. Vercel > New Project > import the repo > **Root Directory: `web`** > Framework: Other > Deploy.
3. On Koyeb set `ALLOWED_ORIGINS=https://your-site.vercel.app` to lock down CORS.

Do not run the downloader itself on Vercel: serverless functions have short timeouts, small response limits and no ffmpeg, so downloads will fail. Keep the API on Koyeb (or any Docker host).
