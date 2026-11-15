"""
ReelGrab - download Instagram, Facebook and TikTok videos/reels.
Web UI + REST API, powered by FastAPI and yt-dlp.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
Docs: http://localhost:8000/docs
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import shutil
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from urllib.parse import quote

# ---------------------------------------------------------------- config
API_KEYS = {k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()}
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
MAX_MB = int(os.getenv("MAX_FILE_MB", "500"))
# Some CDNs (TikTok) refuse browsers: they need cookies/headers and the server's IP. For these hosts only,
# the API hands out a signed relay link that streams the file through the server (no disk, no ffmpeg).
RELAY = os.getenv("RELAY", "true").lower() == "true"
RELAY_HOSTS = tuple(h.strip() for h in os.getenv(
    "RELAY_HOSTS", "tiktok.com,tiktokcdn.com,tiktokv.com,byteoversea.com,ibytedtos.com,musical.ly").split(",") if h.strip())
SECRET = os.getenv("SECRET") or secrets.token_hex(16)
ALLOW_GENERIC = os.getenv("ALLOW_GENERIC", "false").lower() == "true"  # arbitrary-page extractor (riskier)
COOKIES_FILE = os.getenv("COOKIES_FILE", "")  # Netscape cookies.txt (needed for some IG/FB content)
_b64 = os.getenv("COOKIES_B64", "")  # alternative: base64 of cookies.txt as an env var
if _b64.strip():
    import base64
    try:
        _raw = re.sub(r"[^A-Za-z0-9+/_-]", "", _b64)  # drop spaces, quotes, newlines, '='
        _raw = _raw.replace("-", "+").replace("_", "/")
        _txt = base64.b64decode(_raw + "=" * (-len(_raw) % 4)).decode("utf-8")
        if "\t" not in _txt:
            raise ValueError("decoded text is not a cookies file")
        COOKIES_FILE = os.path.join(tempfile.gettempdir(), "cookies.txt")
        with open(COOKIES_FILE, "w") as _f:
            _f.write(_txt)
        print("COOKIES_B64 loaded:", _txt.count("\n"), "lines")
    except Exception as _e:  # never crash the server over bad cookies
        print("WARNING: COOKIES_B64 is invalid and was ignored:", _e)
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
    """Validate the URL and block private/internal hosts (SSRF). Returns hostname."""
    try:
        p = urlparse(url.strip())
        host = (p.hostname or "").lower()
    except Exception:
        raise HTTPException(400, "Invalid URL")
    if p.scheme not in ("http", "https") or not host:
        raise HTTPException(400, "URL must start with http:// or https://")
    try:
        for res in socket.getaddrinfo(host, None):
            if not ipaddress.ip_address(res[4][0]).is_global:
                raise HTTPException(400, "This address is not allowed")
    except socket.gaierror:
        raise HTTPException(400, "Could not resolve host")
    return host


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
        "allowed_extractors": ["default"] if ALLOW_GENERIC else ["default", "-generic"],
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


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _sign(p: str) -> str:
    return hmac.new(SECRET.encode(), p.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(d: dict) -> str:
    p = _b64(json.dumps({**d, "e": int(time.time()) + 3600}).encode())
    return p + "." + _sign(p)


def read_token(t: str) -> dict:
    try:
        p, sig = t.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(p)):
            raise ValueError
        d = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        if d["e"] < time.time():
            raise ValueError
        return d
    except Exception:
        raise HTTPException(403, "Link expired or invalid. Fetch the video again.")


def _row(f, ydl=None):
    u = f.get("url")
    if not u or not u.startswith("http"):
        return None
    vc, ac = f.get("vcodec"), f.get("acodec")
    unk = vc is None and ac is None
    v = unk or vc not in (None, "none")
    a = unk or ac not in (None, "none")
    if not v and not a:
        return None
    row = {
        "id": f.get("format_id"), "ext": f.get("ext"), "height": f.get("height"),
        "note": f.get("format_note") or f.get("format_id"),
        "size": f.get("filesize") or f.get("filesize_approx"),
        "kind": "video" if v and a else "video-only" if v else "audio",
        "stream": (f.get("protocol") or "").startswith(("m3u8", "http_dash", "dash", "ism", "f4m")),
        "url": u,
    }
    host = (urlparse(u).hostname or "").lower()
    if RELAY and ydl is not None and host.endswith(RELAY_HOSTS):
        ck = ydl.cookiejar.get_cookie_header(u) if getattr(ydl, "cookiejar", None) else ""
        row["relay"] = "/api/stream?t=" + make_token({"u": u, "h": f.get("http_headers") or {}, "c": ck})
    return row


def fetch_info(url: str) -> dict:
    opts = {**base_opts(), "skip_download": True, "format": "bv*+ba/b"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    allf = info.get("formats") or [info]
    rows = [r for r in (_row(f, ydl) for f in allf) if r]
    rank = {"video": 0, "video-only": 1, "audio": 2}
    rows.sort(key=lambda r: (rank[r["kind"]], -(r["height"] or 0)))
    best = [r for r in (_row(f, ydl) for f in (info.get("requested_formats") or [info])) if r]
    return {
        "site": info.get("extractor_key"), "id": info.get("id"),
        "title": info.get("title") or (info.get("description") or "")[:80] or "Untitled",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
        "formats": rows, "best": best, "hidden": len(allf) - len(rows),
    }


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


@app.get("/api/stream", tags=["API"], summary="Signed relay for CDNs that block browsers")
async def api_stream(t: str, request: Request, name: str = "video.mp4"):
    d = read_token(t)
    hdr = dict(d.get("h") or {})
    if d.get("c"):
        hdr["Cookie"] = d["c"]
    if request.headers.get("range"):
        hdr["Range"] = request.headers["range"]
    client = httpx.AsyncClient(timeout=30, follow_redirects=False)
    try:
        r = await client.send(client.build_request("GET", d["u"], headers=hdr), stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, "Could not reach the source: " + clean_error(e))
    if r.status_code >= 400:
        code = r.status_code
        await r.aclose()
        await client.aclose()
        raise HTTPException(502, f"Source refused the request ({code})")

    async def close():
        await r.aclose()
        await client.aclose()

    out = {k: r.headers[k] for k in ("content-type", "content-length", "content-range", "accept-ranges") if k in r.headers}
    out["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(safe_name(name.rsplit(".", 1)[0]) + "." + (name.rsplit(".", 1)[-1][:5] if "." in name else "mp4"))
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out, background=BackgroundTask(close))


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
