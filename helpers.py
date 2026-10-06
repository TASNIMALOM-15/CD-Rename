import asyncio
import json
import mimetypes
import os
import re

from PIL import Image, ImageOps

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
    base, dot, ext = s.rpartition(".")
    if not dot or not base or len(ext) > 10:
        base, ext = s, ""
    limit = 200 - (len(ext.encode()) + 1 if ext else 0)
    while len(base.encode()) > limit:
        base = base[:-1]
    return base + ("." + ext if ext else "")


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
                w, h = int(s.get("width", 0)), int(s.get("height", 0))
                rot = 0
                try:
                    rot = int(float(s.get("tags", {}).get("rotate", 0)))
                except Exception:
                    pass
                for sd in s.get("side_data_list", []) or []:
                    if "rotation" in sd:
                        rot = int(float(sd["rotation"]))
                if abs(rot) % 180 == 90:
                    w, h = h, w
                info["width"], info["height"] = w, h
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


# ---------- Thumbnail / GIF helpers ----------
async def _frame_once(src, t, out, size, q):
    if os.path.exists(out):
        os.remove(out)
    await _run(["ffmpeg", "-y", "-ss", f"{max(t, 0):.2f}", "-i", src, "-frames:v", "1",
                "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                "-q:v", str(q), out])
    return os.path.getsize(out) if os.path.exists(out) else 0


async def extract_frame(src, t, out, size, max_bytes=None):
    """ভিডিওর t সেকেন্ডের ফ্রেম jpg আকারে বের করে। সফল হলে True।"""
    for tt in (t, 0.0):
        got = 0
        for q in (3, 6, 10, 15, 22):
            got = await _frame_once(src, tt, out, size, q)
            if got == 0:
                break
            if max_bytes is None or got <= max_bytes:
                return True
        if got:  # ফাইল হয়েছে কিন্তু বেশি বড়
            return False
    return False


def make_thumb_from_image(src, out):
    """ইউজারের ছবি থেকে Telegram-এর উপযোগী thumbnail (<=320px, <200KB, JPEG)।"""
    im = Image.open(src)
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((320, 320))
    q = 90
    while True:
        im.save(out, "JPEG", quality=q)
        if os.path.getsize(out) <= 195 * 1024 or q <= 30:
            break
        q -= 10


async def make_gif(src, starts, dur, out, width=480, fps=12):
    """starts তালিকার প্রতিটি সময় থেকে dur সেকেন্ডের ক্লিপ নিয়ে জুড়ে একটিমাত্র GIF বানায়।"""
    if os.path.exists(out):
        os.remove(out)
    cmd = ["ffmpeg", "-y"]
    for st in starts:
        cmd += ["-ss", f"{st:.2f}", "-t", f"{dur}", "-i", src]
    n = len(starts)
    parts = [f"[{k}:v]fps={fps},scale='min({width},iw)':-2:flags=lanczos,setsar=1[v{k}]"
             for k in range(n)]
    joined = "".join(f"[v{k}]" for k in range(n))
    fg = (";".join(parts) + f";{joined}concat=n={n}:v=1:a=0,split[a][b];"
          "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4[o]")
    cmd += ["-filter_complex", fg, "-map", "[o]", "-an", "-loop", "0", out]
    code, _, err = await _run(cmd)
    if code != 0 or not os.path.exists(out):
        raise RuntimeError("GIF বানানো যায়নি: " + err.decode(errors="ignore")[-200:])
