import os, re, urllib.parse
import requests
import yt_dlp
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Only these sites are accepted, so the server cannot be used as an open proxy.
ALLOWED = re.compile(r"^https?://([\w-]+\.)*(instagram\.com|youtube\.com|youtu\.be|facebook\.com|fb\.watch)/", re.I)
IMAGE_KINDS = {"photo", "dp", "thumb"}


def extract(url, kind):
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
        return ydl.extract_info(url, download=False)


def friendly(e):
    m = str(e).lower()
    if "sign in" in m or "bot" in m or "cookies" in m:
        return "YouTube is blocking this server right now. Try again later."
    if "private" in m or "login" in m:
        return "This post is private or needs a login."
    if "unavailable" in m or "not available" in m:
        return "This video is not available."
    return "Could not fetch this link."


def check(url):
    if not url or not ALLOWED.match(url):
        return False
    return True


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
    q = urllib.parse.urlencode({"url": url, "type": kind})
    stream = f"{base}/api/file?{q}"
    direct = info.get("thumbnail") if kind in IMAGE_KINDS else info.get("url")
    return jsonify(
        title=info.get("title"),
        thumbnail=f"{base}/api/file?{urllib.parse.urlencode({'url': url, 'type': 'thumb'})}"
        if info.get("thumbnail") else None,
        video=None if kind in IMAGE_KINDS else stream,
        servers=[stream + "&dl=1", direct],
    )


@app.get("/api/file")
def file():
    url = request.args.get("url", "").strip()
    kind = request.args.get("type", "video")
    if not check(url):
        return "Unsupported link", 400
    try:
        info = extract(url, kind)
    except Exception:
        return "Could not fetch this link", 422

    target = info.get("thumbnail") if kind in IMAGE_KINDS else info.get("url")
    if not target:
        return "Nothing to download", 404

    headers = dict(info.get("http_headers") or {})
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    r = requests.get(target, headers=headers, stream=True, timeout=30)

    ext = info.get("ext") or "mp4"
    if kind in IMAGE_KINDS:
        ext = "jpg"
    elif kind == "audio":
        ext = "m4a"
    name = re.sub(r"[^\w.-]+", "_", f"pullr_{info.get('id', 'file')}.{ext}")

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
