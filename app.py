"""
ReelGrab - download Instagram, Facebook and TikTok videos/reels.
Web UI + REST API, powered by FastAPI and yt-dlp.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
Docs: http://localhost:8000/docs
"""
import os
import re
import shutil
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

# ---------------------------------------------------------------- config
API_KEYS = {k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()}
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
MAX_MB = int(os.getenv("MAX_FILE_MB", "500"))
COOKIES_FILE = os.getenv("COOKIES_FILE", "")  # Netscape cookies.txt (needed for some IG/FB content)
HAS_FFMPEG = shutil.which("ffmpeg") is not None

PLATFORMS = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.watch", "fb.com"),
    "tiktok": ("tiktok.com",),
}

app = FastAPI(title="ReelGrab API", version="1.0.0")
ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length"],
)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_hits = defaultdict(deque)


# ---------------------------------------------------------------- helpers
def detect_platform(url: str) -> str:
    """Validate the URL and return the platform name. Allowlist prevents SSRF/abuse."""
    try:
        p = urlparse(url.strip())
    except Exception:
        raise HTTPException(400, "Invalid URL")
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "URL must start with http:// or https://")
    host = p.hostname.lower()
    for name, domains in PLATFORMS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    raise HTTPException(400, "Only Instagram, Facebook and TikTok links are supported")


def auth(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    api_key: Optional[str] = None,
):
    key = x_api_key or api_key
    if API_KEYS and not (key and any(secrets.compare_digest(key, k) for k in API_KEYS)):
        raise HTTPException(401, "Missing or invalid API key")
    ident = key or (request.client.host if request.client else "anon")
    now = time.time()
    dq = _hits[ident]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        raise HTTPException(429, "Rate limit exceeded, try again in a minute")
    dq.append(now)
    return key


def base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def clean_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    msg = msg.replace("ERROR: ", "").strip()
    return msg[:300]


def safe_name(s: str) -> str:
    s = re.sub(r"[^\w\-. ]+", "", s or "video", flags=re.UNICODE).strip()
    return (s[:80] or "video")


def fetch_info(url: str) -> dict:
    opts = {**base_opts(), "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):  # carousel / playlist -> first item
        info = next(iter(info["entries"]))
    heights = sorted({f["height"] for f in info.get("formats", []) if f.get("height")}, reverse=True)
    return {
        "platform": detect_platform(url),
        "id": info.get("id"),
        "title": info.get("title") or info.get("description", "")[:80] or "Untitled",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "qualities": heights,
        "ffmpeg": HAS_FFMPEG,
    }


def run_download(url: str, kind: str, quality: Optional[int]) -> tuple[str, str, str]:
    tmp = tempfile.mkdtemp(prefix="reelgrab_")
    opts = {
        **base_opts(),
        "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
        "max_filesize": MAX_MB * 1024 * 1024,
    }
    if kind == "audio":
        opts["format"] = "bestaudio/best"
        if HAS_FFMPEG:
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ]
    else:
        h = f"[height<={quality}]" if quality else ""
        if HAS_FFMPEG:
            opts["format"] = f"bestvideo{h}+bestaudio/best{h}/best"
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = f"best{h}[ext=mp4]/best{h}/best"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get("entries"):
                info = next(iter(info["entries"]))
        files = [os.path.join(tmp, f) for f in os.listdir(tmp)]
        if not files:
            raise RuntimeError("Download produced no file (maybe it exceeds the size limit)")
        path = max(files, key=os.path.getsize)
        ext = os.path.splitext(path)[1]
        return tmp, path, safe_name(info.get("title") or info.get("id")) + ext
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# ---------------------------------------------------------------- API
class InfoRequest(BaseModel):
    url: str


@app.post("/api/info", tags=["API"], summary="Get video metadata and available qualities")
async def api_info(body: InfoRequest, _=Depends(auth)):
    detect_platform(body.url)
    try:
        return await run_in_threadpool(fetch_info, body.url)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(422, clean_error(e))


@app.get("/api/download", tags=["API"], summary="Download a video or audio file")
async def api_download(
    url: str,
    type: str = "video",
    quality: Optional[int] = None,
    _=Depends(auth),
):
    """`type` = video | audio. `quality` = max height in pixels (e.g. 720)."""
    detect_platform(url)
    if type not in ("video", "audio"):
        raise HTTPException(400, "type must be 'video' or 'audio'")
    if not _slots.acquire(blocking=False):
        raise HTTPException(503, "Server busy, try again shortly")
    try:
        tmp, path, name = await run_in_threadpool(run_download, url, type, quality)
    except yt_dlp.utils.DownloadError as e:
        _slots.release()
        raise HTTPException(422, clean_error(e))
    except Exception as e:
        _slots.release()
        raise HTTPException(500, clean_error(e))

    def cleanup():
        shutil.rmtree(tmp, ignore_errors=True)
        _slots.release()

    return FileResponse(path, filename=name, background=BackgroundTask(cleanup))


@app.get("/api/health", tags=["API"])
async def health():
    return {"status": "ok", "ffmpeg": HAS_FFMPEG, "auth_required": bool(API_KEYS)}


# ---------------------------------------------------------------- Web UI
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReelGrab</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#16181d;--muted:#6b7280;--accent:#6d28d9;--border:#e5e7eb;--err:#b91c1c}
@media(prefers-color-scheme:dark){:root{--bg:#0e0f13;--card:#181a20;--text:#eef0f4;--muted:#9aa1ae;--accent:#a78bfa;--border:#2a2d36;--err:#f87171}}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--text);padding:20px}
main{max-width:560px;margin:0 auto}
h1{margin:8px 0 4px}p.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px;margin-bottom:16px}
input,select{width:100%;padding:12px;border-radius:10px;border:1px solid var(--border);background:var(--bg);color:var(--text);font:inherit}
button{padding:12px 16px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button.alt{background:transparent;color:var(--accent);border:1px solid var(--accent)}
button:disabled{opacity:.5}
.row{display:flex;gap:10px;margin-top:10px}.row>*{flex:1}
img{width:100%;border-radius:10px;max-height:320px;object-fit:cover}
.muted{color:var(--muted);font-size:14px}.err{color:var(--err)}
details summary{cursor:pointer;color:var(--muted);font-size:14px}
code{background:var(--bg);padding:2px 6px;border-radius:6px}
</style></head><body><main>
<h1>ReelGrab</h1>
<p class="sub">Instagram, Facebook &amp; TikTok videos and reels.</p>
<div class="card">
  <input id="url" type="url" placeholder="Paste a video or reel link" autocomplete="off">
  <div class="row"><button id="go">Fetch</button></div>
  <details style="margin-top:12px"><summary>API key (if required)</summary>
    <input id="key" placeholder="API key" style="margin-top:8px"></details>
  <p id="msg" class="muted"></p>
</div>
<div id="result" class="card" hidden>
  <img id="thumb" alt="">
  <h3 id="title"></h3><p id="meta" class="muted"></p>
  <select id="quality"></select>
  <div class="row"><button id="dlv">Download video</button><button id="dla" class="alt">Audio (MP3)</button></div>
</div>
<div class="card muted">
  <b>API</b><br>
  <code>POST /api/info</code> {"url": "..."}<br>
  <code>GET /api/download?url=...&amp;type=video&amp;quality=720</code><br>
  Header <code>X-API-Key</code> if enabled. Interactive docs at <a href="/docs">/docs</a>.
</div>
<p class="muted">Only download content you own or have permission to save.</p>
</main>
<script>
const $=id=>document.getElementById(id);
$("key").value=localStorage.getItem("rg_key")||"";
function headers(){const k=$("key").value.trim();localStorage.setItem("rg_key",k);const h={"Content-Type":"application/json"};if(k)h["X-API-Key"]=k;return h}
function say(t,e){$("msg").textContent=t;$("msg").className=e?"err":"muted"}
$("go").onclick=async()=>{
  const url=$("url").value.trim();if(!url)return;
  $("go").disabled=true;say("Fetching info...");$("result").hidden=true;
  try{
    const r=await fetch("/api/info",{method:"POST",headers:headers(),body:JSON.stringify({url})});
    const d=await r.json();if(!r.ok)throw new Error(d.detail||"Request failed");
    $("thumb").src=d.thumbnail||"";$("thumb").hidden=!d.thumbnail;
    $("title").textContent=d.title;
    $("meta").textContent=[d.platform,d.uploader,d.duration?Math.round(d.duration)+"s":""].filter(Boolean).join(" · ");
    const q=$("quality");q.innerHTML="";
    const best=document.createElement("option");best.value="";best.textContent="Best quality";q.appendChild(best);
    d.qualities.forEach(h=>{const o=document.createElement("option");o.value=h;o.textContent=h+"p";q.appendChild(o)});
    $("result").hidden=false;say("");
  }catch(e){say(e.message,true)}
  $("go").disabled=false;
};
async function dl(type){
  const url=$("url").value.trim(),q=$("quality").value;
  say("Preparing download, this can take a moment...");
  try{
    const p=new URLSearchParams({url,type});if(q&&type==="video")p.set("quality",q);
    const h=headers();delete h["Content-Type"];
    const r=await fetch("/api/download?"+p,{headers:h});
    if(!r.ok){const d=await r.json();throw new Error(d.detail||"Download failed")}
    const blob=await r.blob();
    const cd=r.headers.get("content-disposition")||"";
    const m=cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
    const a=document.createElement("a");a.href=URL.createObjectURL(blob);
    a.download=m?decodeURIComponent(m[1]):"video";a.click();
    URL.revokeObjectURL(a.href);say("Done.");
  }catch(e){say(e.message,true)}
}
$("dlv").onclick=()=>dl("video");$("dla").onclick=()=>dl("audio");
</script></body></html>"""


if os.path.isdir(WEB_DIR):
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index():
        return PAGE
