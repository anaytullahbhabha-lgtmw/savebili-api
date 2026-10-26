import os
import re
import json
import time
import tempfile
import pathlib
import mimetypes
import shutil
import subprocess

from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from pydantic import BaseModel

import requests
import yt_dlp

import bili_fallback


APP_NAME = "SaveBili API"
MAX_URL_LEN = 2000
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "2"))

_last_by_ip = {}


app = FastAPI(title=APP_NAME, version="2.2.0")

allowed = os.getenv("CORS_ORIGINS", "*")
origins = [x.strip() for x in allowed.split(",") if x.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class ParseRequest(BaseModel):
    url: str


def valid_bilibili(url: str) -> bool:
    if not url or len(url) > MAX_URL_LEN:
        return False

    try:
        p = urlparse(url)
        host = (p.hostname or "").lower().rstrip(".")

        return (
            p.scheme in ("http", "https")
            and (
                host == "bilibili.com"
                or host.endswith(".bilibili.com")
                or host == "bilibili.tv"
                or host.endswith(".bilibili.tv")
            )
        )
    except Exception:
        return False


def normalize_bilibili_input(value: str) -> str:
    """Accept bare BV/AV IDs and b23.tv links, return a full Bilibili URL.

    The frontend placeholders suggest bare BV IDs work, so normalize:
    - 'BV17x411w7KC' -> 'https://www.bilibili.com/video/BV17x411w7KC'
    - 'av170001' / 'AV170001' -> 'https://www.bilibili.com/video/av170001'
    - full URLs pass through unchanged
    """
    value = (value or "").strip()

    if not value:
        return value

    # Already a URL — pass through, except Bilibili's /en/ locale
    # prefix: yt-dlp returns metadata but zero streams for
    # bilibili.com/en/video/... URLs (verified 2026-10-26 with
    # BV1MtnG6NEGF), while the standard form works. Strip it so
    # every downstream path (parse, downloads) sees the
    # canonical form. bilibili.tv is left untouched (unverified).
    if value.startswith(("http://", "https://")):
        try:
            p = urlparse(value)
            host = (p.hostname or "").lower()
            if (
                host == "bilibili.com"
                or host.endswith(".bilibili.com")
            ) and p.path.startswith("/en/"):
                value = urlunparse(
                    p._replace(path=p.path[3:])
                )
        except Exception:
            pass
        return value

    # Bare BV ID (BV + 10 alphanumeric chars)
    if re.match(r"^BV[a-zA-Z0-9]{10}$", value):
        return f"https://www.bilibili.com/video/{value}"

    # Bare AV ID (av/AV + digits)
    if re.match(r"^[aA][vV]\d+$", value):
        return f"https://www.bilibili.com/video/{value.lower()}"

    # b23.tv short link without scheme
    if value.startswith("b23.tv/"):
        return f"https://{value}"

    return value


def check_rate(ip):
    now = time.time()
    last = _last_by_ip.get(ip, 0)

    if now - last < RATE_LIMIT_SECONDS:
        raise HTTPException(
            429,
            "Please wait a moment before making another request."
        )

    _last_by_ip[ip] = now


def ydl_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_args": {
            "bilibili": {
                "prefer_multi_flv": False
            }
        },
    }


def human_duration(seconds):
    if seconds is None:
        return ""

    try:
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60

        return (
            f"{h}:{m:02d}:{s:02d}"
            if h
            else f"{m}:{s:02d}"
        )
    except:
        return ""


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": APP_NAME
    }


def build_formats(info):
    """Normalize one extractor's raw formats into download options."""
    formats = []
    seen = set()

    raw = info.get("formats") or []

    for f in raw:

        fid = str(f.get("format_id") or "")
        ext = f.get("ext") or ""

        v = f.get("vcodec") not in (None, "none")
        a = f.get("acodec") not in (None, "none")

        if not (v or a):
            continue

        if not f.get("url"):
            continue

        if fid in seen:
            continue

        seen.add(fid)

        height = f.get("height")
        fps = f.get("fps")

        if v and a:
            label = f"{height}p" if height else "Video"

        elif v:
            label = (
                f"{height}p Video"
                if height
                else "Video"
            )

        else:
            label = "Audio"

        if fps and v:
            label += f" {int(fps)}fps"

        formats.append({
            "id": fid,
            "format_id": fid,
            "label": label,
            "ext": ext,
            "url": f.get("url"),
            "filesize": (
                f.get("filesize")
                or f.get("filesize_approx")
            ),
            "height": height,
            "fps": fps,
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
        })

    # Prefer formats with both video/audio,
    # then highest resolution, then audio.
    formats.sort(
        key=lambda x: (
            x["acodec"] != "none",
            x["height"] or 0,
            x["vcodec"] != "none"
        ),
        reverse=True
    )

    return formats


@app.post("/api/parse")
def parse(req: ParseRequest, ip: str = "unknown"):

    check_rate(ip)

    url = normalize_bilibili_input(req.url.strip())

    if not valid_bilibili(url):
        raise HTTPException(
            400,
            "Please enter a valid public Bilibili URL."
        )

    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            info = ydl.extract_info(
                url,
                download=False
            )
        used_fallback = False

    except Exception as e:
        # yt-dlp extraction broken since Bilibili dropped
        # __INITIAL_STATE__ — fall back to the web APIs.
        try:
            info = bili_fallback.fallback_info(url)
            used_fallback = True
        except HTTPException:
            raise
        except Exception:
            msg = str(e)

            if "login" in msg.lower():
                msg = (
                    "This video appears to require login "
                    "or is not publicly accessible."
                )

            raise HTTPException(
                422,
                msg[:500]
            )

    formats = build_formats(info)

    if not formats and not used_fallback:
        # yt-dlp sometimes succeeds with metadata but zero usable
        # streams (verified 2026-10-26: /en/ video URLs reached
        # yt-dlp before normalization stripped the prefix).
        # Fall back to the web APIs instead of reporting an
        # empty quality list. If the fallback also yields
        # nothing, the empty list stands (honest error).
        try:
            info = bili_fallback.fallback_info(url)
            formats = build_formats(info)
        except Exception:
            pass

    return {
        "title": info.get("title") or "Bilibili video",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "duration_string": human_duration(
            info.get("duration")
        ),
        "uploader": (
            info.get("uploader")
            or info.get("channel")
        ),
        "download_options": formats[:30],
    }


@app.get("/api/download")
def download(
    url: str,
    filename: str = "video.mp4"
):

    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            400,
            "Invalid media URL."
        )

    # Allow Bilibili and known Bilibili media/CDN hosts.
    host = (
        urlparse(url).hostname
        or ""
    ).lower().rstrip(".")

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
        raise HTTPException(
            400,
            "Media host is not allowed."
        )

    import requests

    try:
        r = requests.get(
            url,
            stream=True,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        r.raise_for_status()

    except Exception:
        raise HTTPException(
            502,
            "Unable to fetch the media resource."
        )

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
            for chunk in r.iter_content(
                1024 * 1024
            ):
                if chunk:
                    yield chunk
        finally:
            r.close()

    return StreamingResponse(
        iterator(),
        media_type=ctype,
        headers={
            "Content-Disposition":
                f'attachment; filename="{safe}"'
        }
    )


# ============================================================
# MERGED VIDEO + AUDIO DOWNLOAD
# ============================================================

@app.get("/api/download-merged")
def download_merged(
    url: str,
    video_format_id: str,
    audio_format_id: str = "",
    filename: str = "video.mp4"
):

    # Normalize bare BV/AV IDs, then validate Bilibili page URL
    url = normalize_bilibili_input(url)
    if not valid_bilibili(url):
        raise HTTPException(
            400,
            "Please enter a valid public Bilibili URL."
        )

    # Basic validation for format IDs
    if not video_format_id or (
        not audio_format_id and video_format_id != "durl"
    ):
        raise HTTPException(
            400,
            "Both video_format_id and audio_format_id are required."
        )

    # Create temporary directory
    tmpdir = tempfile.mkdtemp(
        prefix="savebili-"
    )

    output_template = os.path.join(
        tmpdir,
        "%(id)s.%(ext)s"
    )

    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,

        # Select video + audio
        "format": (
            f"{video_format_id}+"
            f"{audio_format_id}"
        ),

        # Ask FFmpeg to create MP4
        "merge_output_format": "mp4",

        "outtmpl": output_template,

        "socket_timeout": 30,
        "retries": 2,

        "extractor_args": {
            "bilibili": {
                "prefer_multi_flv": False
            }
        },
    }

    try:

        # yt-dlp downloads both streams
        # and FFmpeg merges them.
        with yt_dlp.YoutubeDL(
            ydl_options
        ) as ydl:

            ydl.extract_info(
                url,
                download=True
            )

        # Find the final merged MP4
        mp4_files = list(
            pathlib.Path(tmpdir).glob("*.mp4")
        )

        if not mp4_files:
            raise Exception(
                "FFmpeg did not create the merged MP4 file."
            )

        final_file = mp4_files[0]

        # Sanitize filename
        safe_filename = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            filename
        )[:120]

        if not safe_filename:
            safe_filename = "video.mp4"

        if not safe_filename.lower().endswith(".mp4"):
            safe_filename += ".mp4"

        # Send file and remove temporary directory
        # after response has finished.
        return FileResponse(
            path=str(final_file),
            media_type="video/mp4",
            filename=safe_filename,
            background=BackgroundTask(
                shutil.rmtree,
                tmpdir,
                ignore_errors=True
            )
        )

    except Exception as e:

        # Fall back to direct stream download + ffmpeg merge.
        try:
            fb = bili_fallback.fallback_info(url)
            # Progressive html5 file: already combined video+audio,
            # no merge needed — just download and return it.
            if video_format_id == "durl":
                _, durl = bili_fallback.playurl_html5(
                    url, fb.get("_fallback_cid")
                )
                if durl and durl[0].get("url"):
                    direct = os.path.join(
                        tmpdir, "direct.mp4"
                    )
                    bili_fallback.download_stream(
                        durl[0]["url"], direct
                    )
                    safe_filename = re.sub(
                        r"[^A-Za-z0-9._-]+",
                        "_",
                        filename,
                    )[:120]
                    if not safe_filename:
                        safe_filename = "video.mp4"
                    if not safe_filename.lower().endswith(
                        ".mp4"
                    ):
                        safe_filename += ".mp4"
                    return FileResponse(
                        path=direct,
                        media_type="video/mp4",
                        filename=safe_filename,
                        background=BackgroundTask(
                            shutil.rmtree,
                            tmpdir,
                            ignore_errors=True,
                        ),
                    )
            dash = bili_fallback.playurl(
                url, fb.get("_fallback_cid")
            )
            videos = dash.get("video") or []
            audios = dash.get("audio") or []
            if videos and audios:
                vstream = next(
                    (
                        v for v in videos
                        if str(v.get("id"))
                        == str(video_format_id)
                    ),
                    max(
                        videos,
                        key=lambda x: x.get("height") or 0,
                    ),
                )
                astream = next(
                    (
                        a for a in audios
                        if str(a.get("id"))
                        == str(audio_format_id)
                    ),
                    audios[0],
                )
                vpath = os.path.join(tmpdir, "v.mp4")
                apath = os.path.join(tmpdir, "a.m4a")
                bili_fallback.download_stream(
                    vstream.get("baseUrl")
                    or vstream.get("base_url"),
                    vpath,
                )
                bili_fallback.download_stream(
                    astream.get("baseUrl")
                    or astream.get("base_url"),
                    apath,
                )
                merged = os.path.join(tmpdir, "merged.mp4")
                proc = subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", vpath, "-i", apath,
                        "-c", "copy", merged,
                    ],
                    capture_output=True,
                    timeout=300,
                )
                if proc.returncode == 0 and os.path.exists(
                    merged
                ):
                    safe_filename = re.sub(
                        r"[^A-Za-z0-9._-]+",
                        "_",
                        filename,
                    )[:120]
                    if not safe_filename:
                        safe_filename = "video.mp4"
                    if not safe_filename.lower().endswith(
                        ".mp4"
                    ):
                        safe_filename += ".mp4"
                    return FileResponse(
                        path=merged,
                        media_type="video/mp4",
                        filename=safe_filename,
                        background=BackgroundTask(
                            shutil.rmtree,
                            tmpdir,
                            ignore_errors=True,
                        ),
                    )
        except Exception:
            pass

        # Cleanup if something fails
        shutil.rmtree(
            tmpdir,
            ignore_errors=True
        )

        raise HTTPException(
            422,
            f"Unable to download and merge video: {str(e)[:500]}"
        )


# ============================================================
# AUDIO DOWNLOAD (M4A + MP3)
# ============================================================

@app.get("/api/download-audio")
def download_audio(
    url: str,
    audio_format_id: str = None,
    filename: str = "audio.mp3",
    format: str = "mp3",
    ip: str = "unknown"
):

    check_rate(ip)

    # Normalize bare BV/AV IDs, then validate Bilibili page URL
    url = normalize_bilibili_input(url)
    if not valid_bilibili(url):
        raise HTTPException(
            400,
            "Please enter a valid public Bilibili URL."
        )

    # Validate output format
    format = (format or "mp3").lower().strip()
    if format not in ("mp3", "m4a"):
        raise HTTPException(
            400,
            "Format must be 'mp3' or 'm4a'."
        )

    # Create temporary directory
    tmpdir = tempfile.mkdtemp(
        prefix="savebili-audio-"
    )

    output_template = os.path.join(
        tmpdir,
        "%(id)s.%(ext)s"
    )

    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "outtmpl": output_template,
        "extractor_args": {
            "bilibili": {
                "prefer_multi_flv": False
            }
        },
    }

    # Select specific audio format if provided, else best audio
    if audio_format_id:
        ydl_options["format"] = audio_format_id
    else:
        ydl_options["format"] = "bestaudio/best"

    # Add MP3 conversion via FFmpeg postprocessor
    if format == "mp3":
        ydl_options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(
                url,
                download=True
            )

        # Find the final audio file
        if format == "mp3":
            audio_files = list(
                pathlib.Path(tmpdir).glob("*.mp3")
            )
        else:
            audio_files = list(
                pathlib.Path(tmpdir).glob("*.m4a")
            )
            # Fallback: any audio file
            if not audio_files:
                for ext in ("*.aac", "*.opus", "*.webm"):
                    audio_files = list(
                        pathlib.Path(tmpdir).glob(ext)
                    )
                    if audio_files:
                        break

        if not audio_files:
            raise Exception(
                f"Could not extract {format.upper()} audio from this video."
            )

        final_file = audio_files[0]

        # Sanitize filename
        safe_filename = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            filename
        )[:120]

        if not safe_filename:
            safe_filename = f"audio.{format}"

        # Ensure correct extension
        if not safe_filename.lower().endswith(f".{format}"):
            # Remove old extension and add correct one
            safe_filename = re.sub(
                r"\.[a-zA-Z0-9]+$",
                "",
                safe_filename
            ) + f".{format}"

        media_type = (
            "audio/mpeg"
            if format == "mp3"
            else "audio/mp4"
        )

        return FileResponse(
            path=str(final_file),
            media_type=media_type,
            filename=safe_filename,
            background=BackgroundTask(
                shutil.rmtree,
                tmpdir,
                ignore_errors=True
            )
        )

    except HTTPException:
        shutil.rmtree(
            tmpdir,
            ignore_errors=True
        )
        raise

    except Exception as e:
        # Fall back to direct audio-stream download + ffmpeg.
        try:
            fb = bili_fallback.fallback_info(url)
            dash = bili_fallback.playurl(
                url, fb.get("_fallback_cid")
            )
            audios = dash.get("audio") or []
            if audios:
                astream = next(
                    (
                        a for a in audios
                        if audio_format_id
                        and str(a.get("id"))
                        == str(audio_format_id)
                    ),
                    audios[0],
                )
                raw = os.path.join(tmpdir, "raw.m4a")
                bili_fallback.download_stream(
                    astream.get("baseUrl")
                    or astream.get("base_url"),
                    raw,
                )
                safe_filename = re.sub(
                    r"[^A-Za-z0-9._-]+",
                    "_",
                    filename,
                )[:120]
                if not safe_filename:
                    safe_filename = f"audio.{format}"
                if format == "mp3":
                    out = os.path.join(tmpdir, "out.mp3")
                    proc = subprocess.run(
                        [
                            "ffmpeg", "-y", "-i", raw,
                            "-vn", "-codec:a", "libmp3lame",
                            "-q:a", "2", out,
                        ],
                        capture_output=True,
                        timeout=300,
                    )
                    if proc.returncode != 0:
                        raise Exception(
                            "ffmpeg mp3 conversion failed"
                        )
                    final_file = out
                    media_type = "audio/mpeg"
                else:
                    final_file = raw
                    media_type = "audio/mp4"
                if not safe_filename.lower().endswith(
                    f".{format}"
                ):
                    safe_filename = re.sub(
                        r"\.[a-zA-Z0-9]+$",
                        "",
                        safe_filename,
                    ) + f".{format}"
                return FileResponse(
                    path=final_file,
                    media_type=media_type,
                    filename=safe_filename,
                    background=BackgroundTask(
                        shutil.rmtree,
                        tmpdir,
                        ignore_errors=True,
                    ),
                )
        except Exception:
            pass
        shutil.rmtree(
            tmpdir,
            ignore_errors=True
        )
        raise HTTPException(
            422,
            f"Unable to download audio: {str(e)[:500]}"
        )


# ============================================================
# VIDEO METADATA VIEWER
# ============================================================

@app.get("/api/metadata")
def metadata(url: str, ip: str = "unknown"):

    check_rate(ip)

    url = normalize_bilibili_input((url or "").strip())

    if not valid_bilibili(url):
        raise HTTPException(
            400,
            "Please enter a valid public Bilibili URL."
        )

    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            info = ydl.extract_info(
                url,
                download=False
            )

    except Exception as e:
        # yt-dlp extraction broken since Bilibili dropped
        # __INITIAL_STATE__ — fall back to the web APIs.
        try:
            info = bili_fallback.fallback_info(url)
        except HTTPException:
            raise
        except Exception:
            msg = str(e)

            if "login" in msg.lower():
                msg = (
                    "This video appears to require login "
                    "or is not publicly accessible."
                )

            raise HTTPException(
                422,
                msg[:500]
            )

    thumbs = []

    for t in (info.get("thumbnails") or [])[-6:]:
        if t.get("url"):
            thumbs.append({
                "url": t.get("url"),
                "width": t.get("width"),
                "height": t.get("height"),
            })

    heights = sorted({
        f.get("height")
        for f in (info.get("formats") or [])
        if f.get("height")
    })

    raw_date = info.get("upload_date") or ""

    upload_date = (
        f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        if len(raw_date) == 8 and raw_date.isdigit()
        else raw_date
    )

    return {
        "id": info.get("id"),
        "title": info.get("title") or "Bilibili video",
        "webpage_url": info.get("webpage_url") or url,
        "uploader": (
            info.get("uploader")
            or info.get("channel")
        ),
        "uploader_id": info.get("uploader_id"),
        "duration": info.get("duration"),
        "duration_string": human_duration(
            info.get("duration")
        ),
        "upload_date": upload_date,
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "description": (
            info.get("description") or ""
        )[:2000],
        "tags": info.get("tags") or [],
        "categories": info.get("categories") or [],
        "thumbnail": info.get("thumbnail"),
        "thumbnails": thumbs,
        "available_heights": heights,
        "format_count": len(
            info.get("formats") or []
        ),
        "has_subtitles": bool(
            info.get("subtitles")
            or info.get("automatic_captions")
        ),
    }


# ============================================================
# SUBTITLE LIST + DOWNLOAD (SRT)
# ============================================================

def _bili_sub_json(sub_url):
    """Fetch a Bilibili subtitle JSON file."""
    r = requests.get(
        sub_url,
        timeout=20,
        headers={
            "Referer": "https://www.bilibili.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    )
    r.raise_for_status()
    return r.json()


def _subtitle_tracks(info):
    tracks = []

    for kind in ("subtitles", "automatic_captions"):
        subs = info.get(kind) or {}

        for lang, items in subs.items():
            for it in items or []:
                if it.get("url"):
                    tracks.append({
                        "lang": lang,
                        "name": it.get("name") or lang,
                        "ext": it.get("ext") or "vtt",
                        "automatic": (
                            kind == "automatic_captions"
                        ),
                    })
                    break

    # Dedupe by language, preferring manual tracks.
    seen = {}

    for t in tracks:
        if (
            t["lang"] not in seen
            or not t["automatic"]
        ):
            seen[t["lang"]] = t

    return list(seen.values())


@app.get("/api/subtitles")
def subtitles_list(url: str, ip: str = "unknown"):

    check_rate(ip)

    url = normalize_bilibili_input((url or "").strip())

    if not valid_bilibili(url):
        raise HTTPException(
            400,
            "Please enter a valid public Bilibili URL."
        )

    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            info = ydl.extract_info(
                url,
                download=False
            )

    except Exception as e:
        # Fall back to the player API subtitle list.
        try:
            parts = bili_fallback.pagelist(url)
            if not parts:
                raise HTTPException(
                    422,
                    "Unable to fetch video info from Bilibili.",
                )
            tracks = bili_fallback.subtitle_list(
                url, parts[0].get("cid")
            )
            return {
                "title": (
                    parts[0].get("part") or "Bilibili video"
                ),
                "subtitles": tracks,
            }
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                422,
                str(e)[:500]
            )

    return {
        "title": info.get("title") or "Bilibili video",
        "subtitles": _subtitle_tracks(info),
    }


@app.get("/api/subtitles/download")
def subtitles_download(
    url: str,
    lang: str = "en",
    ip: str = "unknown"
):

    check_rate(ip)

    url = normalize_bilibili_input((url or "").strip())
    lang = (lang or "en").strip()

    if not valid_bilibili(url):
        raise HTTPException(
            400,
            "Please enter a valid public Bilibili URL."
        )

    if not re.match(r"^[A-Za-z0-9_-]{2,12}$", lang):
        raise HTTPException(
            400,
            "Invalid subtitle language code."
        )

    tmpdir = tempfile.mkdtemp(
        prefix="savebili-sub-"
    )

    try:
        opts = ydl_opts()
        opts.update({
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "sublangs": [lang],
            "convertsubtitles": "srt",
            "outtmpl": os.path.join(
                tmpdir,
                "%(id)s.%(ext)s"
            ),
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                url,
                download=True
            )

        srt_files = list(
            pathlib.Path(tmpdir).glob("*.srt")
        )

        if not srt_files:
            raise HTTPException(
                404,
                (
                    f"No subtitles found for "
                    f"language '{lang}' on this video."
                )
            )

        srt_file = srt_files[0]

        title = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            info.get("title") or "subtitles"
        )[:80]

        filename = f"{title}.{lang}.srt"

        return FileResponse(
            path=str(srt_file),
            media_type="text/plain",
            filename=filename,
            background=BackgroundTask(
                shutil.rmtree,
                tmpdir,
                ignore_errors=True
            )
        )

    except HTTPException:
        shutil.rmtree(
            tmpdir,
            ignore_errors=True
        )
        raise

    except Exception as e:
        # Fall back to the player API: fetch the subtitle JSON
        # for the requested language and convert it to SRT.
        try:
            parts = bili_fallback.pagelist(url)
            if parts:
                tracks = bili_fallback.subtitle_list(
                    url, parts[0].get("cid")
                )
                match = None
                for t in tracks:
                    if t["lang"].lower().startswith(
                        lang.lower()[:2]
                    ):
                        match = t
                        break
                if not match and tracks:
                    match = tracks[0]
                if match:
                    sub_json = _bili_sub_json(match["url"])
                    srt_text = bili_fallback.json_to_srt(
                        sub_json
                    )
                    if srt_text.strip():
                        srt_path = os.path.join(
                            tmpdir, "subtitles.srt"
                        )
                        with open(
                            srt_path, "w", encoding="utf-8"
                        ) as f:
                            f.write(srt_text)
                        title = re.sub(
                            r"[^A-Za-z0-9._-]+",
                            "_",
                            parts[0].get("part")
                            or "subtitles",
                        )[:80]
                        return FileResponse(
                            path=srt_path,
                            media_type="text/plain",
                            filename=(
                                f"{title}.{lang}.srt"
                            ),
                            background=BackgroundTask(
                                shutil.rmtree,
                                tmpdir,
                                ignore_errors=True
                            )
                        )
        except HTTPException:
            shutil.rmtree(
                tmpdir,
                ignore_errors=True
            )
            raise
        except Exception:
            pass
        shutil.rmtree(
            tmpdir,
            ignore_errors=True
        )
        raise HTTPException(
            422,
            f"Unable to download subtitles: {str(e)[:500]}"
        )
