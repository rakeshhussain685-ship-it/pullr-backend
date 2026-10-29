import os, re, time, urllib.parse
import requests
import yt_dlp
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Only these sites are accepted, so the server cannot be used as an open proxy.
ALLOWED = re.compile(r"^https?://([\w-]+\.)*(instagram\.com|youtube\.com|youtu\.be|facebook\.com|fb\.watch)/", re.I)
IMAGE_KINDS = {"photo", "dp", "thumb"}
_cache = {}  # (url, kind) -> (time, info); saves repeating the slow lookup


def extract(url, kind):
    key = (url, kind)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    fmt = "bestaudio[ext=m4a]/bestaudio" if kind == "audio" else "best[ext=mp4]/best"
    opts = {
        "quiet": True,
        "noplaylist": True,
        "format": fmt,
        "skip_download": True,
        "extractor_retries": 2,
        # Different YouTube player clients are blocked less often on server IPs.
        "extractor_args": {"youtube": {"player_client": ["tv", "android_vr", "web_safari", "mweb"]}},
    }
    cookies = os.environ.get("COOKIES_FILE")  # optional, helps with Instagram
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if len(_cache) > 50:
        _cache.clear()
    _cache[key] = (time.time(), info)
    return info


def friendly(e):
    m = str(e).lower()
    if "sign in" in m or "bot" in m or "cookies" in m:
        return "This site is blocking the server right now. Try again later."
    if "private" in m or "login" in m:
        return "This post is private or needs a login."
    if "unavailable" in m or "not available" in m:
        return "This video is not available."
    return "Could not fetch this link."


def check(url):
    return bool(url) and bool(ALLOWED.match(url))


def short_side(f):
    """Quality in 'p' units. Uses the shorter side so vertical reels count as 720p, not 1280p."""
    vals = [v for v in (f.get("width"), f.get("height")) if v]
    return min(vals) if vals else None


def progressive(info):
    """Formats that already hold video and audio together and can be streamed over plain HTTP."""
    out = []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue
        if f.get("protocol") not in ("https", "http", None):
            continue
        p = short_side(f)
        if p:
            out.append((p, f))
    return out


def qualities(info):
    best = {}
    for p, f in progressive(info):
        if p >= 140 and (p not in best or (f.get("tbr") or 0) > (best[p].get("tbr") or 0)):
            best[p] = f
    return sorted(best)


def pick(info, kind, p):
    """Return the direct media URL and extension to stream for this request."""
    if kind in IMAGE_KINDS:
        return info.get("thumbnail"), "jpg"
    if kind == "audio":
        return info.get("url"), info.get("ext") or "m4a"
    prog = progressive(info)
    if prog and p:
        exact = [f for q, f in prog if q == p]
        below = [(q, f) for q, f in prog if q <= p]
        if exact:
            f = max(exact, key=lambda x: x.get("tbr") or 0)
        elif below:
            f = max(below, key=lambda x: (x[0], x[1].get("tbr") or 0))[1]
        else:
            f = min(prog, key=lambda x: x[0])[1]
        return f["url"], f.get("ext") or "mp4"
    if prog:
        f = max(prog, key=lambda x: (x[0], x[1].get("tbr") or 0))[1]
        return f["url"], f.get("ext") or "mp4"
    return info.get("url"), info.get("ext") or "mp4"


@app.get("/")
def home():
    return "Pullr API is running"


@app.get("/api")
def api():
    url = request.args.get("url", "").strip()
    kind = request.args.get("type", "video")
    if not check(url):
        return jsonify(error="Unsupported link"), 400
    try:
        info = extract(url, kind)
    except Exception as e:
        app.logger.error("extract failed for %s: %s", url, e)
        return jsonify(error=friendly(e)), 422

    base = request.host_url.rstrip("/")

    def link(extra, kind_=kind):
        q = urllib.parse.urlencode({"url": url, "type": kind_, **extra})
        return f"{base}/api/file?{q}"

    thumb = link({}, "thumb") if info.get("thumbnail") else None
    if kind in IMAGE_KINDS:
        return jsonify(title=info.get("title"), thumbnail=thumb, video=None,
                       qualities=[{"label": "Download", "url": link({"dl": 1})}])
    if kind == "audio":
        return jsonify(title=info.get("title"), thumbnail=thumb, video=None,
                       qualities=[{"label": "Audio", "url": link({"dl": 1})}])

    ps = qualities(info)
    if ps:
        preview_p = next((p for p in ps if p >= 360), ps[-1])
        items = [{"label": f"{p}p", "url": link({"p": p, "dl": 1})} for p in ps]
        video = link({"p": preview_p})
    else:
        items = [{"label": "Download", "url": link({"dl": 1})}]
        video = link({})
    return jsonify(title=info.get("title"), thumbnail=thumb, video=video, qualities=items)


@app.get("/api/file")
def file():
    url = request.args.get("url", "").strip()
    kind = request.args.get("type", "video")
    p = request.args.get("p", type=int)
    if not check(url):
        return "Unsupported link", 400
    try:
        info = extract(url, kind)
    except Exception as e:
        app.logger.error("extract failed for %s: %s", url, e)
        return friendly(e), 422

    target, ext = pick(info, kind, p)
    if not target:
        return "Nothing to download", 404

    headers = dict(info.get("http_headers") or {})
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    r = requests.get(target, headers=headers, stream=True, timeout=30)

    tag = f"_{p}p" if p and kind not in IMAGE_KINDS and kind != "audio" else ""
    name = re.sub(r"[^\w.-]+", "_", f"pullr_{info.get('id', 'file')}{tag}.{ext}")

    out = {"Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
           "Accept-Ranges": "bytes"}
    for h in ("Content-Length", "Content-Range"):
        if h in r.headers:
            out[h] = r.headers[h]
    if request.args.get("dl"):
        out["Content-Disposition"] = f'attachment; filename="{name}"'
    return Response(r.iter_content(64 * 1024), status=r.status_code, headers=out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
