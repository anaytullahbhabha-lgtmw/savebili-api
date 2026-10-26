"""Bilibili web-API fallback for SaveBili.

Bilibili stopped embedding window.__INITIAL_STATE__ in video pages
served to plain clients (late Sep 2026), so yt-dlp's bilibili
extractor fails with "Unable to extract initial state". This module
uses Bilibili's public web APIs (pagelist / playurl / view / player)
which need no login. It runs when yt-dlp extraction raises, or
when yt-dlp returns metadata but no usable streams;
public response shapes of every endpoint stay unchanged.
"""

import re
import os
import time

import requests
from fastapi import HTTPException

BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json",
}

_session = requests.Session()
_session.headers.update(BILI_HEADERS)

# Optional Bilibili login: Bilibili serves 1080p DASH streams only
# to logged-in sessions; anonymous requests cap at 480p DASH /
# 720p progressive. When BILI_SESSDATA is set (Render env var, never
# in code), its cookie is sent with every API call and stream
# download, unlocking the account's qualities. Without it the
# fallback works exactly as before. The value is never logged.
_sessdata = os.getenv("BILI_SESSDATA", "").strip()
if _sessdata:
    _session.cookies.set(
        "SESSDATA", _sessdata, domain=".bilibili.com"
    )


def _get(path, params):
    """GET one Bilibili API path; returns parsed data or None."""
    try:
        r = _session.get(
            f"https://api.bilibili.com{path}",
            params=params,
            timeout=20,
        )
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("code") != 0:
            return None
        return body.get("data")
    except Exception:
        return None


def extract_ids(url):
    """Return (bvid, aid) parsed from a Bilibili video URL."""
    m = re.search(r"/video/(BV[a-zA-Z0-9]{10})", url or "")
    if m:
        return m.group(1), None
    m = re.search(r"/video/av(\d+)", url or "")
    if m:
        return None, m.group(1)
    return None, None


def id_params(url):
    bvid, aid = extract_ids(url)
    if bvid:
        return {"bvid": bvid}
    if aid:
        return {"aid": aid}
    return None


def pagelist(url):
    """Parts list: [{cid, page, part, duration, dimension}]."""
    params = id_params(url)
    if not params:
        return []
    data = _get("/x/player/pagelist", params)
    return data if isinstance(data, list) else []


def view(url):
    """Full metadata from the view API (may be None if banned)."""
    params = id_params(url)
    if not params:
        return None
    data = _get("/x/web-interface/view", params)
    return data if isinstance(data, dict) else None


def playurl(url, cid):
    """DASH streams for one part: {'video': [...], 'audio': [...]}."""
    params = id_params(url)
    if not params or not cid:
        return {}
    params.update({
        "cid": cid,
        "qn": 80,
        "fnval": 16,
        "fourk": 0,
    })
    data = _get("/x/player/playurl", params)
    if not isinstance(data, dict):
        return {}
    return data.get("dash") or {}


def playurl_html5(url, cid):
    """Progressive (durl) streams via the html5 player variant.

    Anonymous DASH streams cap at 480p; the html5 variant returns
    a combined video+audio file whose actual resolution varies by
    video. Returns (quality_code, durl_list).

    NOTE: quality_code is the video's availability tier, NOT the
    delivered stream's resolution. Verified 2026-10-26: a tier-112
    video's anonymous progressive file was actually 512x288, so the
    tier code must never be mapped to a height claim.
    """
    params = id_params(url)
    if not params or not cid:
        return None, []
    params.update({
        "cid": cid,
        "qn": 80,
        "fnval": 16,
        "platform": "html5",
    })
    data = _get("/x/player/playurl", params)
    if not isinstance(data, dict):
        return None, []
    return data.get("quality"), (data.get("durl") or [])


# NOTE (2026-10-26): Bilibili quality-tier codes used to be mapped to
# pixel heights for the progressive (durl) format. That mapping was WRONG:
# the tier code describes what quality the video is available in, not the
# resolution of the anonymous progressive file actually served (a tier-112
# video served a 512x288 file). The maps were removed; the durl format now
# claims no height and is labelled "Progressive". The widget renders it as
# "Video • <size> • combined", which is honest for every video.


def subtitle_list(url, cid):
    """Subtitle tracks from the player API for one part."""
    params = id_params(url)
    if not params or not cid:
        return []
    params.update({"cid": cid})
    data = _get("/x/player/wbi/v2", params)
    if not isinstance(data, dict):
        return []
    sub = data.get("subtitle") or {}
    tracks = []
    for t in sub.get("subtitles") or []:
        if t.get("subtitle_url"):
            tracks.append({
                "lang": t.get("lan") or "",
                "name": t.get("lan_doc") or t.get("lan") or "",
                "ext": "json",
                "automatic": False,
                "url": t.get("subtitle_url"),
            })
    return tracks


def download_stream(stream_url, dest_path):
    """Download one Bilibili stream URL to a file."""
    with _session.get(
        stream_url,
        stream=True,
        timeout=120,
        headers={"Referer": "https://www.bilibili.com/"},
    ) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
    return dest_path


def srt_timestamp(seconds):
    try:
        total_ms = int(float(seconds) * 1000)
    except Exception:
        total_ms = 0
    h = total_ms // 3600000
    m = (total_ms % 3600000) // 60000
    s = (total_ms % 60000) // 1000
    ms = total_ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def json_to_srt(sub_json):
    """Convert Bilibili subtitle JSON body to SRT text."""
    lines = []
    body = sub_json.get("body") or []
    n = 0
    for cue in body:
        text = (cue.get("content") or "").strip()
        if not text:
            continue
        n += 1
        start = srt_timestamp(cue.get("from", 0))
        end = srt_timestamp(cue.get("to", 0))
        lines.append(f"{n}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def human_duration(seconds):
    if seconds is None:
        return ""
    try:
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return ""


def fallback_info(url):
    """Best-effort metadata + formats without yt-dlp.

    Returns a dict shaped like the subset of yt-dlp info the
    endpoints use, or raises HTTPException(422) when even the
    fallback APIs cannot serve this video.
    """
    parts = pagelist(url)
    if not parts:
        raise HTTPException(
            422,
            "Unable to fetch video info from Bilibili.",
        )
    part = parts[0]
    cid = part.get("cid")

    meta = view(url)
    dash = playurl(url, cid)

    title = "Bilibili video"
    thumbnail = None
    uploader = None
    uploader_id = None
    view_count = like_count = comment_count = None
    description = ""
    upload_date = ""
    tags = []
    categories = []

    if meta:
        title = meta.get("title") or title
        thumbnail = meta.get("pic")
        owner = meta.get("owner") or {}
        uploader = owner.get("name")
        uploader_id = owner.get("mid")
        stat = meta.get("stat") or {}
        view_count = stat.get("view")
        like_count = stat.get("like")
        comment_count = stat.get("reply")
        description = meta.get("desc") or ""
        pubdate = meta.get("pubdate")
        if pubdate:
            try:
                upload_date = time.strftime(
                    "%Y-%m-%d", time.gmtime(int(pubdate))
                )
            except Exception:
                upload_date = ""
        tags = [
            t.get("tag_name")
            for t in (meta.get("tags") or [])
            if t.get("tag_name")
        ]
    else:
        # View API unavailable (rate-limit/ban): fall back to
        # the part name so the tool still returns something real.
        title = part.get("part") or title

    formats = []
    for v in dash.get("video") or []:
        base = v.get("baseUrl") or v.get("base_url")
        if not base:
            continue
        height = v.get("height")
        formats.append({
            "id": str(v.get("id")),
            "format_id": str(v.get("id")),
            "label": f"{height}p Video" if height else "Video",
            "ext": "mp4",
            "url": base,
            "filesize": v.get("bandwidth"),
            "height": height,
            "fps": None,
            "vcodec": v.get("codecs"),
            "acodec": "none",
        })
    for a in dash.get("audio") or []:
        base = a.get("baseUrl") or a.get("base_url")
        if not base:
            continue
        formats.append({
            "id": str(a.get("id")),
            "format_id": str(a.get("id")),
            "label": "Audio",
            "ext": "m4a",
            "url": base,
            "filesize": a.get("bandwidth"),
            "height": None,
            "fps": None,
            "vcodec": "none",
            "acodec": a.get("codecs"),
        })

    # Combined video+audio entry first (best video + best audio),
    # mirroring what the frontend expects from yt-dlp.
    videos = [f for f in formats if f["vcodec"] != "none"]
    audios = [f for f in formats if f["acodec"] != "none"]
    if videos and audios:
        best_v = max(videos, key=lambda x: x["height"] or 0)
        best_a = audios[0]
        formats.insert(0, {
            "id": f"{best_v['id']}+{best_a['id']}",
            "format_id": f"{best_v['id']}+{best_a['id']}",
            "label": (
                f"{best_v['height']}p"
                if best_v["height"]
                else "Video"
            ),
            "ext": "mp4",
            "url": best_v["url"],
            "filesize": best_v.get("filesize"),
            "height": best_v["height"],
            "fps": None,
            "vcodec": best_v["vcodec"],
            "acodec": best_a["acodec"],
        })

    # Progressive html5 file(s): anonymous DASH caps at 480p, but
    # the html5 variant serves a combined file that is often the
    # best actually downloadable file (verified real 720p on one
    # video, 288p on another). Offer it FIRST. Its height is left
    # as None on purpose: the tier code is not the delivered
    # resolution (see playurl_html5 note), so claiming one would
    # mislabel the quality picker.
    try:
        durl_quality, durl = playurl_html5(url, cid)
    except Exception:
        durl_quality, durl = None, []
    durl_formats = []
    for d in durl:
        durl_url = d.get("url")
        if not durl_url:
            continue
        durl_formats.append({
            "id": "durl",
            "format_id": "durl",
            "label": "Progressive",
            "ext": "mp4",
            "url": durl_url,
            "filesize": d.get("size"),
            "height": None,
            "fps": None,
            "vcodec": "combined",
            "acodec": "combined",
        })
    if durl_formats:
        formats = durl_formats + formats

    duration = part.get("duration")
    bvid, aid = extract_ids(url)
    return {
        "id": bvid or aid,
        "title": title,
        "webpage_url": url,
        "uploader": uploader,
        "uploader_id": uploader_id,
        "duration": duration,
        "duration_string": human_duration(duration),
        "upload_date": upload_date,
        "view_count": view_count,
        "like_count": like_count,
        "comment_count": comment_count,
        "description": (description or "")[:2000],
        "tags": tags,
        "categories": categories,
        "thumbnail": thumbnail,
        "thumbnails": (
            [{"url": thumbnail}] if thumbnail else []
        ),
        "formats": formats,
        "subtitles": [],
        "automatic_captions": {},
        "_fallback_cid": cid,
    }
