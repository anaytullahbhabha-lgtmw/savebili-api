import os, re, time, tempfile, pathlib, mimetypes
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import yt_dlp

APP_NAME="SaveBili API"
MAX_URL_LEN=2000
RATE_LIMIT_SECONDS=float(os.getenv("RATE_LIMIT_SECONDS","2"))
_last_by_ip={}

app=FastAPI(title=APP_NAME, version="2.0.0")
allowed=os.getenv("CORS_ORIGINS","*")
origins=[x.strip() for x in allowed.split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET","POST"], allow_headers=["*"])

class ParseRequest(BaseModel):
    url: str

def valid_bilibili(url:str)->bool:
    if not url or len(url)>MAX_URL_LEN: return False
    try:
        p=urlparse(url)
        host=(p.hostname or "").lower().rstrip(".")
        return p.scheme in ("http","https") and (host=="bilibili.com" or host.endswith(".bilibili.com") or host=="bilibili.tv" or host.endswith(".bilibili.tv"))
    except Exception:
        return False

def check_rate(ip):
    now=time.time()
    last=_last_by_ip.get(ip,0)
    if now-last<RATE_LIMIT_SECONDS:
        raise HTTPException(429, "Please wait a moment before making another request.")
    _last_by_ip[ip]=now

def ydl_opts():
    return {
        "quiet":True,
        "no_warnings":True,
        "noplaylist":True,
        "skip_download":True,
        "socket_timeout":20,
        "retries":2,
        "extractor_args":{"bilibili":{"prefer_multi_flv":False}},
    }

def human_duration(seconds):
    if seconds is None:return ""
    try:
        seconds=int(seconds); h=seconds//3600; m=(seconds%3600)//60; s=seconds%60
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except:return ""

@app.get("/health")
def health(): return {"ok":True,"service":APP_NAME}

@app.post("/api/parse")
def parse(req:ParseRequest, ip:str="unknown"):
    check_rate(ip)
    url=req.url.strip()
    if not valid_bilibili(url):
        raise HTTPException(400,"Please enter a valid public Bilibili URL.")
    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            info=ydl.extract_info(url,download=False)
    except Exception as e:
        msg=str(e)
        if "login" in msg.lower(): msg="This video appears to require login or is not publicly accessible."
        raise HTTPException(422, msg[:500])

    formats=[]
    seen=set()
    raw=info.get("formats") or []
    for f in raw:
        fid=str(f.get("format_id") or "")
        ext=f.get("ext") or ""
        v=f.get("vcodec") not in (None,"none")
        a=f.get("acodec") not in (None,"none")
        if not (v or a): continue
        if not f.get("url"): continue
        if fid in seen: continue
        seen.add(fid)
        height=f.get("height")
        fps=f.get("fps")
        if v and a:
            label=f"{height}p" if height else "Video"
        elif v:
            label=f"{height}p Video" if height else "Video"
        else:
            label="Audio"
        if fps and v: label+=f" {int(fps)}fps"
        formats.append({
            "id":fid,
            "format_id":fid,
            "label":label,
            "ext":ext,
            "url":f.get("url"),
            "filesize":f.get("filesize") or f.get("filesize_approx"),
            "height":height,
            "fps":fps,
            "vcodec":f.get("vcodec"),
            "acodec":f.get("acodec"),
        })
    # Prefer formats with both video/audio, then highest resolution, then audio.
    formats.sort(key=lambda x: (x["acodec"]!="none", x["height"] or 0, x["vcodec"]!="none"), reverse=True)
    return {
        "title":info.get("title") or "Bilibili video",
        "thumbnail":info.get("thumbnail"),
        "duration":info.get("duration"),
        "duration_string":human_duration(info.get("duration")),
        "uploader":info.get("uploader") or info.get("channel"),
        "download_options":formats[:30],
    }

@app.get("/api/download")
def download(url: str, filename: str = "video.mp4"):
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid media URL.")

    # Allow Bilibili and known Bilibili media/CDN hosts.
           host = (urlparse(url).hostname or "").lower().rstrip(".")
    allowed_host = (
        host == "bilibili.com"
        or host.endswith(".bilibili.com")
        or host == "bilibili.tv"
        or host.endswith(".bilibili.tv")
        or host == "hdslb.com"
        or host.endswith(".hdslb.com")
        or host == "bilivideo.com"
        or host.endswith(".bilivideo.com")
        or host == "akamaized.net"
        or host.endswith(".akamaized.net")
    )

    if not allowed_host:
        raise HTTPException(400, "Media host is not allowed.")

    import requests

    try:
        r = requests.get(
            url,
            stream=True,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()
    except Exception:
        raise HTTPException(502, "Unable to fetch the media resource.")

    safe = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        filename
    )[:120] or "video.mp4"

    ctype = (
        r.headers.get("content-type")
        or mimetypes.guess_type(safe)[0]
        or "application/octet-stream"
    )

    def iterator():
        try:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    yield chunk
        finally:
            r.close()

    return StreamingResponse(
        iterator(),
        media_type=ctype,
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"'
        }
    )
