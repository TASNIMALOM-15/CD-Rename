import asyncio
import json
import mimetypes
import os
import re

from PIL import Image

VIDEO_EXT = {"mp4", "mkv", "webm", "avi", "mov", "flv", "m4v", "3gp", "wmv", "ts", "mpeg", "mpg"}
AUDIO_EXT = {"mp3", "m4a", "aac", "wav", "ogg", "opus", "flac", "wma", "oga", "amr"}
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "bmp", "tiff", "tif", "ico", "gif"}

VIDEO_TARGETS = ["mp4", "mkv", "webm", "avi", "mov", "flv", "gif"]
AUDIO_TARGETS = ["mp3", "m4a", "aac", "wav", "ogg", "opus", "flac"]
IMAGE_TARGETS = ["jpg", "png", "webp", "bmp", "gif", "tiff", "ico", "pdf"]

VIDEO_STREAM = {"mp4", "mov", "m4v", "mkv", "webm"}

_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p"]
VARGS = {
    "mp4": _X264 + ["-c:a", "aac", "-movflags", "+faststart"],
    "mkv": _X264 + ["-c:a", "aac"],
    "mov": _X264 + ["-c:a", "aac"],
    "flv": _X264 + ["-c:a", "aac"],
    "webm": ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
             "-b:v", "1M", "-c:a", "libopus"],
    "avi": ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame"],
    "gif": ["-vf", "fps=12,scale=480:-1:flags=lanczos", "-an", "-t", "30"],
}
AARGS = {
    "mp3": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"],
    "m4a": ["-vn", "-c:a", "aac", "-b:a", "192k"],
    "aac": ["-vn", "-c:a", "aac", "-b:a", "192k"],
    "wav": ["-vn", "-c:a", "pcm_s16le"],
    "ogg": ["-vn", "-c:a", "libvorbis", "-q:a", "5"],
    "opus": ["-vn", "-c:a", "libopus", "-b:a", "128k"],
    "flac": ["-vn", "-c:a", "flac"],
}
PIL_FMT = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "bmp": "BMP",
           "gif": "GIF", "tiff": "TIFF", "ico": "ICO", "pdf": "PDF"}

MEDIA_ATTRS = ("document", "video", "audio", "voice", "animation", "video_note", "photo", "sticker")


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .")
    return s[:120]


def split_name(name: str):
    base, dot, ext = name.rpartition(".")
    if not dot or not base:
        return name, ""
    return base, ext.lower()


def join_name(base: str, ext: str) -> str:
    return f"{base}.{ext}" if ext else base


def norm(ext: str) -> str:
    return {"jpeg": "jpg", "tif": "tiff"}.get(ext, ext)


def category(ext: str) -> str:
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    return "other"


def targets_for(ext: str):
    cat = category(ext)
    if cat == "video":
        t = VIDEO_TARGETS + AUDIO_TARGETS
    elif cat == "audio":
        t = AUDIO_TARGETS
    elif cat == "image":
        t = IMAGE_TARGETS + (["mp4"] if ext == "gif" else [])
    else:
        t = []
    return [x for x in t if x != norm(ext)]


def get_media(msg):
    for attr in MEDIA_ATTRS:
        m = getattr(msg, attr, None)
        if m:
            return attr, m
    return None, None


def file_name_of(msg) -> str:
    kind, m = get_media(msg)
    name = getattr(m, "file_name", None)
    mime = getattr(m, "mime_type", None)
    if not name:
        default = {"video": "mp4", "animation": "mp4", "video_note": "mp4", "audio": "mp3",
                   "voice": "ogg", "photo": "jpg", "sticker": "webp"}.get(kind)
        ext = default
        if not ext and mime:
            g = mimetypes.guess_extension(mime)
            ext = g.lstrip(".") if g else None
        name = f"{kind}_{msg.id}.{ext or 'bin'}"
    elif "." not in name and mime:
        g = mimetypes.guess_extension(mime)
        if g:
            name += g
    return safe_name(name)


async def _run(cmd):
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, out, err


async def probe(path):
    info = {"duration": 0, "width": 0, "height": 0}
    try:
        code, out, _ = await _run(["ffprobe", "-v", "error", "-print_format", "json",
                                   "-show_format", "-show_streams", path])
        d = json.loads(out)
        info["duration"] = int(float(d.get("format", {}).get("duration", 0) or 0))
        for s in d.get("streams", []):
            if s.get("codec_type") == "video":
                info["width"] = int(s.get("width", 0))
                info["height"] = int(s.get("height", 0))
                break
    except Exception:
        pass
    return info


async def make_thumb(path):
    thumb = os.path.join(os.path.dirname(path), "thumb.jpg")
    for ss in ("1", "0"):
        await _run(["ffmpeg", "-y", "-ss", ss, "-i", path, "-frames:v", "1",
                    "-vf", "scale=320:-2", thumb])
        if os.path.exists(thumb) and os.path.getsize(thumb) > 0:
            return thumb
    return None


def _pillow(src, dst, ext):
    im = Image.open(src)
    if ext in ("jpg", "bmp", "pdf") and im.mode != "RGB":
        im = im.convert("RGB")
    if ext == "ico":
        im.thumbnail((256, 256))
    im.save(dst, PIL_FMT[ext])


async def convert(src, dst, src_ext, dst_ext):
    if category(src_ext) == "image" and dst_ext in PIL_FMT:
        await asyncio.to_thread(_pillow, src, dst, dst_ext)
        return
    args = VARGS.get(dst_ext) or AARGS.get(dst_ext)
    if args is None:
        raise ValueError(f"'{dst_ext}' ফরমেট সাপোর্টেড নয়")
    code, _, err = await _run(["ffmpeg", "-y", "-i", src, *args, dst])
    if code != 0:
        raise RuntimeError("ffmpeg error: " + err.decode(errors="ignore")[-300:])
