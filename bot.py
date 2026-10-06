import asyncio
import itertools
import json
import logging
import os
import random
import shutil
import tempfile
import time

from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM,
                            KeyboardButton, ReplyKeyboardMarkup)

import helpers as H

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
MAX_BYTES = int(os.getenv("MAX_FILE_MB", "1500")) * 1024 * 1024
PORT = int(os.getenv("PORT", "10000"))
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "settings.json")
sem = asyncio.Semaphore(int(os.getenv("MAX_JOBS", "2")))

M_DOC = "🔄 doc2org_mode"
M_REN = "💳 rename_mode"
M_THUMB = "🌠 C_Thumb_mode"
M_GIF = "⏩ Fast_GIF_mode"
MODE_BY_LABEL = {M_DOC: "doc2org", M_REN: "rename", M_THUMB: "thumb", M_GIF: "gif"}
LABEL_BY_MODE = {v: k for k, v in MODE_BY_LABEL.items()}
MODE_KB = ReplyKeyboardMarkup(
    [[KeyboardButton(M_DOC), KeyboardButton(M_REN)],
     [KeyboardButton(M_THUMB), KeyboardButton(M_GIF)]], resize_keyboard=True)

GIF_DURATIONS = [4, 8, 12, 16, 20]
NEW_ACTS = {"ta", "tm", "tp", "to", "gd", "gt", "gc", "gx"}

user_mode = {}     # uid -> "doc2org" | "rename" | "thumb" | "gif"
gif_default = {}   # uid -> (মোট সেকেন্ড, প্রতি ক্লিপ সেকেন্ড, ক্লিপ সংখ্যা)
pending = {}       # uid -> {"action", "msg_id", "name"}   (rename mode)
collect_buf = {}   # uid -> {"ids": [...], "task": Task}   (একসাথে পাঠানো ভিডিও জমানো)
batches = {}       # bid -> ব্যাচের তথ্য
active_thumb = {}  # uid -> bid   (চলমান thumbnail সেশন)
bid_counter = itertools.count(1)
tasks = set()


class SessionEnd(Exception):
    pass


# ---------- সেটিংস সেভ/লোড ----------
def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        for k, v in d.get("modes", {}).items():
            if v in LABEL_BY_MODE:
                user_mode[int(k)] = v
        for k, v in d.get("gif_default", {}).items():
            gif_default[int(k)] = tuple(int(x) for x in v)
    except Exception:
        pass


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"modes": {str(k): v for k, v in user_mode.items()},
                       "gif_default": {str(k): list(v) for k, v in gif_default.items()}}, f)
    except Exception:
        log.warning("settings save failed")


def allowed(uid):
    return not ALLOWED or uid in ALLOWED


def spawn(coro, b=None):
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)
    if b is not None:
        b["task"] = t
    return t


async def prog(cur, total, status, label, st):
    now = time.time()
    if now - st["t"] < 5:
        return
    st["t"] = now
    try:
        await status.edit_text(f"{label}... {cur * 100 // total if total else 0}%")
    except Exception:
        pass


def fmt_kb(msg_id, prefix, ext):
    ts = H.targets_for(ext)
    if not ts:
        return None
    btns = [IKB(t.upper(), callback_data=f"{prefix}:{msg_id}:{t}") for t in ts]
    return IKM([btns[i:i + 3] for i in range(0, len(btns), 3)])


async def deliver(c, chat_id, path, name, as_photo, status, reply_to=None):
    base, ext = H.split_name(name)
    kw = dict(progress=prog, progress_args=(status, "⬆️ আপলোড", {"t": 0}))
    if reply_to:
        kw["reply_to_message_id"] = reply_to
    if ext in H.VIDEO_STREAM:
        i = await H.probe(path)
        thumb = await H.make_thumb(path)
        await c.send_video(chat_id, path, caption=name, duration=i["duration"], width=i["width"],
                           height=i["height"], thumb=thumb, supports_streaming=True, **kw)
    elif ext in H.AUDIO_EXT:
        i = await H.probe(path)
        await c.send_audio(chat_id, path, title=base, duration=i["duration"], **kw)
    elif as_photo and ext == "gif":
        await c.send_animation(chat_id, path, caption=name, **kw)
    elif as_photo and ext in ("jpg", "jpeg", "png", "bmp"):
        try:
            await c.send_photo(chat_id, path, **kw)
        except Exception:
            await c.send_document(chat_id, path, force_document=True, **kw)
    else:
        await c.send_document(chat_id, path, force_document=True, **kw)


async def run_job(c, chat_id, src, new_base, new_ext, status, as_photo=False, reply_to=None):
    orig_ext = H.split_name(H.file_name_of(src))[1]
    final_name = H.join_name(new_base, new_ext)
    wd = tempfile.mkdtemp(prefix="job_")
    try:
        async with sem:
            src_path = await c.download_media(
                src, file_name=os.path.join(wd, "src" + ("." + orig_ext if orig_ext else "")),
                progress=prog, progress_args=(status, "⬇️ ডাউনলোড", {"t": 0}))
            out = os.path.join(wd, final_name)
            if H.norm(new_ext) == H.norm(orig_ext):
                os.rename(src_path, out)
            else:
                await status.edit_text("🔄 কনভার্ট হচ্ছে...")
                await H.convert(src_path, out, orig_ext, new_ext)
                os.remove(src_path)
            await status.edit_text("⬆️ আপলোড হচ্ছে...")
            await deliver(c, chat_id, out, final_name, as_photo, status, reply_to)
        await status.delete()
    except Exception as e:
        log.exception("job failed")
        try:
            await status.edit_text(f"❌ ব্যর্থ হয়েছে: {e}")
        except Exception:
            pass
    finally:
        shutil.rmtree(wd, ignore_errors=True)


async def get_src(c, chat_id, msg_id):
    src = await c.get_messages(chat_id, msg_id)
    if not src or src.empty or H.get_media(src)[0] is None:
        return None
    return src


# =====================================================================
#  নতুন: ব্যাচ জমানো (একসাথে ফরওয়ার্ড করা ভিডিও এক দলে ধরা)
# =====================================================================
def is_video(kind, ext):
    return kind == "video" or (kind == "document" and H.category(ext) == "video")


def resolve(b, val):
    w = b.get("wait")
    if w is not None and not w.done():
        w.set_result(val)
        return True
    return False


async def wait_user(b, timeout=900):
    b["wait"] = asyncio.get_running_loop().create_future()
    try:
        return await asyncio.wait_for(b["wait"], timeout)
    except asyncio.TimeoutError:
        raise SessionEnd("⏰ অনেকক্ষণ উত্তর না পাওয়ায় কাজ বন্ধ করা হলো।")
    finally:
        b["wait"] = None


async def collect(c, m, mode):
    uid = m.from_user.id
    buf = collect_buf.setdefault(uid, {"ids": [], "task": None})
    buf["ids"].append(m.id)
    if buf["task"]:
        buf["task"].cancel()
    buf["task"] = asyncio.create_task(_flush(c, uid, m.chat.id, mode))


async def _flush(c, uid, chat_id, mode):
    try:
        await asyncio.sleep(2.0)   # শেষ ভিডিওর ২ সেকেন্ড পর পর্যন্ত অপেক্ষা
    except asyncio.CancelledError:
        return
    buf = collect_buf.pop(uid, None)
    if not buf:
        return
    try:
        await show_prompt(c, uid, chat_id, sorted(set(buf["ids"])), mode)
    except Exception:
        log.exception("show_prompt failed")


def gif_options(total):
    return [(s, total // s) for s in range(2, total // 2 + 1) if total % s == 0]


def gif_dur_kb(bid):
    return IKM([[IKB(f"{d} seconds", callback_data=f"gd:{bid}:{d}")] for d in GIF_DURATIONS])


def gif_opt_kb(bid, total, save_def):
    rows = [[IKB(f"{s}_sec | {n}_clip", callback_data=f"gc:{bid}:{total}:{s}")]
            for s, n in gif_options(total)]
    label = "⭐ Default করুন: ON ✅" if save_def else "⭐ Default করুন: OFF"
    rows.append([IKB(label, callback_data=f"gt:{bid}:{total}")])
    return IKM(rows)


async def show_prompt(c, uid, chat_id, ids, mode):
    n = len(ids)
    bid = str(next(bid_counter))
    b = {"uid": uid, "chat_id": chat_id, "ids": ids, "mode": mode, "wait": None,
         "save_def": False, "started": False, "manual": False, "task": None}
    batches[bid] = b
    if mode == "thumb":
        if n == 1:
            l1, l2 = "🤖 Random/Auto_Thumb", "🥶 Manual_Thumb"
        else:
            l1, l2 = "🤖 Auto_Thumb_All", "🥶 Manual_Thumb_One"
        kb = IKM([[IKB(l1, callback_data=f"ta:{bid}")], [IKB(l2, callback_data=f"tm:{bid}")]])
        await c.send_message(chat_id, f"🎬 {n}টি ভিডিও পাওয়া গেছে। থাম্বনেইল কীভাবে বদলাবেন?",
                             reply_markup=kb)
    else:
        d = gif_default.get(uid)
        if d:
            total, s, cnt = d
            b["started"] = True
            await c.send_message(
                chat_id, f"⚙️ Default সেটিং চলছে: {total} সেকেন্ড • {s}_sec | {cnt}_clip\n"
                         "(বদলাতে বা বাদ দিতে: /gifdefault)")
            spawn(gif_runner(c, bid, s, cnt), b)
        else:
            await c.send_message(chat_id, f"🎬 {n}টি ভিডিও পাওয়া গেছে।\n⏱ কত সেকেন্ডের GIF চান?",
                                 reply_markup=gif_dur_kb(bid))


def cancel_sessions(uid):
    buf = collect_buf.pop(uid, None)
    if buf and buf.get("task"):
        buf["task"].cancel()
    for bid, b in list(batches.items()):
        if b["uid"] != uid:
            continue
        if b.get("wait") is not None:
            resolve(b, ("cancel",))
        elif b.get("task"):
            b["task"].cancel()
        else:
            batches.pop(bid, None)


# =====================================================================
#  C_Thumb_mode
# =====================================================================
def random_times(dur):
    if dur >= 3:
        zone = dur / 3
        return [k * zone + random.uniform(zone * 0.1, zone * 0.9) for k in range(3)]
    if dur > 0:
        return [dur * 0.2, dur * 0.5, dur * 0.8]
    return [0.0]


async def gen_frames(path, dur, wd, rnd):
    frames = []
    for k, t in enumerate(random_times(dur), 1):
        prev = os.path.join(wd, f"p{rnd}_{k}.jpg")
        th = os.path.join(wd, f"t{rnd}_{k}.jpg")
        ok1 = await H.extract_frame(path, t, prev, 640)
        ok2 = await H.extract_frame(path, t, th, 320, max_bytes=190 * 1024)
        if ok1 and ok2:
            frames.append((prev, th))
    return frames


async def send_with_thumb(c, chat_id, path, name, thumb, status):
    i = await H.probe(path)
    await c.send_video(chat_id, path, caption=name, duration=i["duration"], width=i["width"],
                       height=i["height"], thumb=thumb, supports_streaming=True,
                       progress=prog, progress_args=(status, "⬆️ আপলোড", {"t": 0}))


async def download_video(c, src, wd, name, status):
    async with sem:
        path = await c.download_media(
            src, file_name=os.path.join(wd, name),
            progress=prog, progress_args=(status, "⬇️ ডাউনলোড", {"t": 0}))
    if not path:
        raise RuntimeError("ভিডিও ডাউনলোড হয়নি")
    return path


async def thumb_auto_one(c, b, bid, src, i, total, wd):
    chat_id = b["chat_id"]
    name = H.file_name_of(src)
    status = await c.send_message(chat_id, f"⏳ ভিডিও {i}/{total}: ডাউনলোড হচ্ছে...")
    path = await download_video(c, src, wd, name, status)
    info = await H.probe(path)
    rnd = 0
    while True:
        rnd += 1
        await status.edit_text(f"🖼 ভিডিও {i}/{total}: থাম্বনেইল বানানো হচ্ছে...")
        frames = await gen_frames(path, info["duration"], wd, rnd)
        if not frames:
            raise RuntimeError("ভিডিও থেকে ছবি বের করা যায়নি")
        media = []
        for k, (pv, _) in enumerate(frames, 1):
            media.append(await c.send_photo(chat_id, pv, caption=f"Thumb_{k}"))
        row = [IKB(f"Thumb_{k}", callback_data=f"tp:{bid}:{k}") for k in range(1, len(frames) + 1)]
        kb = IKM([row, [IKB("Other❗", callback_data=f"to:{bid}")]])
        prompt = await c.send_message(chat_id, f"👆 ভিডিও {i}/{total}: কোন ছবিটি থাম্বনেইল হবে?",
                                      reply_markup=kb)
        try:
            res = await wait_user(b)
        finally:
            try:
                await c.delete_messages(chat_id, [x.id for x in media] + [prompt.id])
            except Exception:
                pass
        if res[0] == "cancel":
            raise SessionEnd("❎ বাতিল করা হয়েছে।")
        if res[0] == "other":
            continue
        thumb = frames[res[1] - 1][1]
        break
    await status.edit_text(f"⬆️ ভিডিও {i}/{total}: আপলোড হচ্ছে...")
    async with sem:
        await send_with_thumb(c, chat_id, path, name, thumb, status)
    await status.delete()


async def thumb_manual_one(c, b, bid, src, i, total, wd):
    chat_id = b["chat_id"]
    name = H.file_name_of(src)
    try:
        await c.copy_message(chat_id, chat_id, src.id)
    except Exception:
        pass
    await c.send_message(
        chat_id, f"🖼 ভিডিও {i}/{total}: এই ভিডিওর জন্য থাম্বনেইল (ছবি) পাঠান।\nবাতিল: /cancel",
        reply_to_message_id=src.id)
    res = await wait_user(b)
    if res[0] == "cancel":
        raise SessionEnd("❎ বাতিল করা হয়েছে।")
    img_msg = res[1]
    status = await c.send_message(chat_id, f"⏳ ভিডিও {i}/{total}: প্রসেস হচ্ছে...")
    img_path = await c.download_media(img_msg, file_name=os.path.join(wd, "userimg"))
    if not img_path:
        raise RuntimeError("ছবি ডাউনলোড হয়নি")
    thumb = os.path.join(wd, "user_thumb.jpg")
    await asyncio.to_thread(H.make_thumb_from_image, img_path, thumb)
    path = await download_video(c, src, wd, name, status)
    await status.edit_text(f"⬆️ ভিডিও {i}/{total}: আপলোড হচ্ছে...")
    async with sem:
        await send_with_thumb(c, chat_id, path, name, thumb, status)
    await status.delete()


async def thumb_runner(c, bid, how):
    b = batches.get(bid)
    if not b:
        return
    uid, chat_id = b["uid"], b["chat_id"]
    total = len(b["ids"])
    try:
        for i, mid in enumerate(b["ids"], 1):
            src = await get_src(c, chat_id, mid)
            if not src:
                await c.send_message(chat_id, f"⚠️ ভিডিও {i} পাওয়া যায়নি, বাদ দেওয়া হলো।")
                continue
            wd = tempfile.mkdtemp(prefix="th_")
            try:
                if how == "auto":
                    await thumb_auto_one(c, b, bid, src, i, total, wd)
                else:
                    await thumb_manual_one(c, b, bid, src, i, total, wd)
            except (SessionEnd, asyncio.CancelledError):
                raise
            except Exception as e:
                log.exception("thumb failed")
                await c.send_message(chat_id, f"❌ ভিডিও {i} ব্যর্থ হয়েছে: {e}")
            finally:
                shutil.rmtree(wd, ignore_errors=True)
        await c.send_message(chat_id, "✅ সব কাজ শেষ!")
    except SessionEnd as e:
        await c.send_message(chat_id, str(e))
    except asyncio.CancelledError:
        try:
            await c.send_message(chat_id, "❎ বাতিল করা হয়েছে।")
        except Exception:
            pass
    finally:
        active_thumb.pop(uid, None)
        batches.pop(bid, None)


# =====================================================================
#  Fast_GIF_mode
# =====================================================================
def pick_starts(dur, clip, count):
    if dur <= 0:
        return [0.0]
    zone = dur / count
    out = []
    for k in range(count):
        lo = k * zone
        hi = max(lo, (k + 1) * zone - clip)
        out.append(random.uniform(lo, hi))
    return out


def fmt_time(t):
    t = int(t)
    h, r = divmod(t, 3600)
    mi, s = divmod(r, 60)
    return f"{h}:{mi:02d}:{s:02d}" if h else f"{mi:02d}:{s:02d}"


async def gif_one(c, chat_id, path, name, s, n, wd, status):
    dur = (await H.probe(path))["duration"]
    base = H.split_name(name)[0]
    clip = float(s)
    if dur and dur < s:
        clip, count = float(dur), 1
    elif dur:
        count = max(1, min(n, dur // s))
    else:
        count = 1
    total = int(clip * count)
    if count < n:
        await c.send_message(
            chat_id, f"ℹ️ ভিডিওটি ছোট, তাই {count}টি ক্লিপে মোট {total} সেকেন্ডের GIF হবে।")
    starts = pick_starts(dur, clip, count)
    await status.edit_text(f"🎞 {count}টি ক্লিপ জুড়ে GIF বানানো হচ্ছে...")
    out = os.path.join(wd, H.safe_name(f"{base}_gif.gif"))
    for width, fps in ((480, 12), (360, 10), (270, 8)):   # বেশি বড় হলে ছোট করে আবার
        await H.make_gif(path, starts, clip, out, width, fps)
        if os.path.getsize(out) <= 45 * 1024 * 1024:
            break
    times = ", ".join(fmt_time(x) for x in starts)
    cap = f"🎞 {name}\n{count} ক্লিপ × {int(clip)} sec = {total} sec\nশুরুর সময়: {times}"
    await c.send_animation(chat_id, out, caption=cap[:1000], duration=total,
                           progress=prog, progress_args=(status, "⬆️ আপলোড", {"t": 0}))
    os.remove(out)


async def gif_runner(c, bid, s, n):
    b = batches.get(bid)
    if not b:
        return
    chat_id = b["chat_id"]
    total = len(b["ids"])
    try:
        for i, mid in enumerate(b["ids"], 1):
            src = await get_src(c, chat_id, mid)
            if not src:
                await c.send_message(chat_id, f"⚠️ ভিডিও {i} পাওয়া যায়নি, বাদ দেওয়া হলো।")
                continue
            name = H.file_name_of(src)
            wd = tempfile.mkdtemp(prefix="gif_")
            status = await c.send_message(chat_id, f"⏳ ভিডিও {i}/{total}: ডাউনলোড হচ্ছে...")
            try:
                async with sem:
                    path = await c.download_media(
                        src, file_name=os.path.join(wd, name),
                        progress=prog, progress_args=(status, "⬇️ ডাউনলোড", {"t": 0}))
                    if not path:
                        raise RuntimeError("ভিডিও ডাউনলোড হয়নি")
                    await gif_one(c, chat_id, path, name, s, n, wd, status)
                await status.delete()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("gif failed")
                try:
                    await status.edit_text(f"❌ ভিডিও {i} ব্যর্থ হয়েছে: {e}")
                except Exception:
                    pass
            finally:
                shutil.rmtree(wd, ignore_errors=True)
        await c.send_message(chat_id, "✅ সব GIF পাঠানো শেষ!")
    except asyncio.CancelledError:
        try:
            await c.send_message(chat_id, "❎ বাতিল করা হয়েছে।")
        except Exception:
            pass
    finally:
        batches.pop(bid, None)


# =====================================================================
#  হ্যান্ডলার
# =====================================================================
async def cmd_start(c, m):
    if not allowed(m.from_user.id):
        return
    cur = LABEL_BY_MODE[user_mode.get(m.from_user.id, "doc2org")]
    await m.reply(
        "👋 স্বাগতম!\n\n"
        f"{M_DOC} — ডকুমেন্ট ফরওয়ার্ড করলে আসল রূপে ফেরত পাবেন\n"
        f"{M_REN} — ফাইলের নাম/ফরমেট পরিবর্তন\n"
        f"{M_THUMB} — ভিডিওর থাম্বনেইল পরিবর্তন\n"
        f"{M_GIF} — ভিডিও থেকে র‍্যান্ডম GIF ক্লিপ\n\n"
        f"এখন চালু: {cur}", reply_markup=MODE_KB)


async def cmd_cancel(c, m):
    uid = m.from_user.id
    pending.pop(uid, None)
    cancel_sessions(uid)
    await m.reply("❎ বাতিল করা হয়েছে।")


async def cmd_gifdefault(c, m):
    d = gif_default.get(m.from_user.id)
    if d:
        await m.reply(f"⭐ বর্তমান Default: {d[0]} সেকেন্ড • {d[1]}_sec | {d[2]}_clip",
                      reply_markup=IKM([[IKB("❌ Default বাদ দিন", callback_data="gx:0")]]))
    else:
        await m.reply("কোনো Default সেট করা নেই।\n\nসেট করতে: ⏩ Fast_GIF_mode এ ভিডিও পাঠান → "
                      "সেকেন্ড বেছে নিন → '⭐ Default করুন' বাটনটি ON করে ক্লিপ বাটনে চাপুন।")


async def on_text(c, m):
    uid = m.from_user.id
    if not allowed(uid):
        return
    t = m.text.strip()
    if t in MODE_BY_LABEL:
        cancel_sessions(uid)
        pending.pop(uid, None)
        user_mode[uid] = MODE_BY_LABEL[t]
        save_settings()
        extra = ""
        if MODE_BY_LABEL[t] == "gif":
            d = gif_default.get(uid)
            extra = (f"\n⭐ Default: {d[0]} সেকেন্ড • {d[1]}_sec | {d[2]}_clip (বদলাতে /gifdefault)"
                     if d else "\nভিডিও পাঠান (একসাথে অনেকগুলোও পাঠাতে পারেন)।")
        elif MODE_BY_LABEL[t] == "thumb":
            extra = "\nভিডিও পাঠান (একসাথে অনেকগুলোও পাঠাতে পারেন)।"
        await m.reply(f"✅ {t} চালু হয়েছে।{extra}")
        return
    p = pending.get(uid)
    if not p or p["action"] not in ("ask_name", "ask_name_overall"):
        await m.reply("ফাইল ফরওয়ার্ড করুন, অথবা /start দিন।")
        return
    src = await get_src(c, m.chat.id, p["msg_id"])
    if not src:
        pending.pop(uid, None)
        await m.reply("⚠️ ফাইলটি পাওয়া যায়নি, আবার ফরওয়ার্ড করুন।")
        return
    _, ext = H.split_name(H.file_name_of(src))
    new_base = H.safe_name(t)
    if ext and new_base.lower().endswith("." + ext):
        new_base = new_base[:-(len(ext) + 1)]
    if not new_base:
        await m.reply("⚠️ নামটি বৈধ নয়, আবার পাঠান।")
        return
    if p["action"] == "ask_name":
        pending.pop(uid, None)
        status = await m.reply("⏳ প্রসেস হচ্ছে...")
        await run_job(c, m.chat.id, src, new_base, ext, status)
    else:
        kb = fmt_kb(p["msg_id"], "co", ext)
        if not kb:
            pending.pop(uid, None)
            status = await m.reply("এই ফাইলের কনভার্ট ফরমেট নেই, শুধু নাম বদলানো হচ্ছে...")
            await run_job(c, m.chat.id, src, new_base, ext, status)
            return
        p["action"] = "pick_fmt"
        p["name"] = new_base
        await m.reply(f"📄 নতুন নাম: {new_base}\nএবার ফরমেট বেছে নিন:", reply_markup=kb)


async def on_media_special(c, m, mode, kind, media):
    """C_Thumb_mode ও Fast_GIF_mode এর জন্য"""
    uid = m.from_user.id
    ext = H.split_name(H.file_name_of(m))[1]
    bid = active_thumb.get(uid) if mode == "thumb" else None
    b = batches.get(bid) if bid else None
    if mode == "thumb":
        is_img = kind == "photo" or (kind == "document" and H.category(ext) == "image")
        if b and b.get("manual") and is_img:
            if not resolve(b, ("photo", m)):
                await m.reply("⏳ একটু অপেক্ষা করুন, আগের কাজ চলছে...")
            return
    if not is_video(kind, ext):
        await m.reply("⚠️ এই মোডে শুধু ভিডিও পাঠান।")
        return
    if (getattr(media, "file_size", 0) or 0) > MAX_BYTES:
        await m.reply("⚠️ ফাইলটি অনেক বড়।", quote=True)
        return
    if b:
        await m.reply("⚠️ আগের থাম্বনেইল কাজ চলছে। শেষ হলে পাঠান, অথবা /cancel দিন।")
        return
    await collect(c, m, mode)


async def on_media(c, m):
    uid = m.from_user.id
    if not allowed(uid):
        return
    kind, media = H.get_media(m)
    mode = user_mode.get(uid, "doc2org")
    if mode in ("thumb", "gif"):
        await on_media_special(c, m, mode, kind, media)
        return
    if kind == "sticker" and (media.is_animated):
        await m.reply("⚠️ অ্যানিমেটেড স্টিকার সাপোর্টেড নয়।")
        return
    if (getattr(media, "file_size", 0) or 0) > MAX_BYTES:
        await m.reply("⚠️ ফাইলটি অনেক বড়।", quote=True)
        return
    name = H.file_name_of(m)
    base, ext = H.split_name(name)
    if mode == "doc2org":
        if kind != "document":
            await m.reply("ℹ️ এটি আগে থেকেই সাধারণ আকারে আছে। ডকুমেন্ট ফরওয়ার্ড করুন।")
            return
        status = await m.reply("⏳ প্রসেস হচ্ছে...", quote=True)
        await run_job(c, m.chat.id, m, base, ext, status, as_photo=True, reply_to=m.id)
    else:
        kb = IKM([[IKB("✏️ Change_Name", callback_data=f"nm:{m.id}")],
                  [IKB("🎞 Change_Format", callback_data=f"fm:{m.id}")],
                  [IKB("🔁 Change_Overall", callback_data=f"ov:{m.id}")]])
        await m.reply(f"📄 {name}\nকী করতে চান?", reply_markup=kb, quote=True)


async def on_cb_new(c, q, parts):
    uid = q.from_user.id
    act = parts[0]

    if act == "gx":
        gif_default.pop(uid, None)
        save_settings()
        await q.answer("Default বাদ দেওয়া হয়েছে")
        await q.message.edit_text("✅ Default বাদ দেওয়া হয়েছে। এখন থেকে প্রতিবার বাটন দিয়ে বেছে নিতে হবে।")
        return

    bid = parts[1]
    b = batches.get(bid)
    if not b or b["uid"] != uid:
        await q.answer("⚠️ সেশন শেষ, আবার ভিডিও পাঠান", show_alert=True)
        return

    if act in ("ta", "tm"):
        if b["started"]:
            await q.answer("⏳ ইতিমধ্যে চলছে")
            return
        if active_thumb.get(uid):
            await q.answer("⚠️ আগের কাজ চলছে। শেষ হলে আবার চেষ্টা করুন।", show_alert=True)
            return
        b["started"] = True
        b["manual"] = act == "tm"
        active_thumb[uid] = bid
        await q.answer()
        await q.message.edit_text("▶️ শুরু হচ্ছে...")
        spawn(thumb_runner(c, bid, "auto" if act == "ta" else "manual"), b)

    elif act in ("tp", "to"):
        ok = resolve(b, ("pick", int(parts[2])) if act == "tp" else ("other",))
        await q.answer() if ok else await q.answer("এই বাটন আর কাজ করে না")

    elif act == "gd":
        total = int(parts[2])
        await q.answer()
        await q.message.edit_text(
            f"✂️ {total} সেকেন্ডের জন্য কয়টি ক্লিপ চান?\n"
            "(সব ক্লিপ মিলে মাত্র ১টি GIF হবে)\n\n"
            "⭐ নিচের বাটন ON করে ক্লিপ বাছলে এই সেটিং Default হয়ে যাবে।",
            reply_markup=gif_opt_kb(bid, total, b["save_def"]))

    elif act == "gt":
        total = int(parts[2])
        b["save_def"] = not b["save_def"]
        await q.answer()
        await q.message.edit_reply_markup(gif_opt_kb(bid, total, b["save_def"]))

    elif act == "gc":
        total, s = int(parts[2]), int(parts[3])
        if b["started"]:
            await q.answer("⏳ ইতিমধ্যে চলছে")
            return
        n = total // s
        b["started"] = True
        if b["save_def"]:
            gif_default[uid] = (total, s, n)
            save_settings()
        await q.answer()
        note = "\n⭐ Default হিসেবে সেভ হয়েছে।" if b["save_def"] else ""
        await q.message.edit_text(f"▶️ শুরু হচ্ছে: {total} সেকেন্ড • {s}_sec | {n}_clip{note}")
        spawn(gif_runner(c, bid, s, n), b)


async def on_cb(c, q):
    uid = q.from_user.id
    if not allowed(uid):
        return
    parts = q.data.split(":")
    if parts[0] in NEW_ACTS:
        await on_cb_new(c, q, parts)
        return
    await q.answer()
    act, msg_id = parts[0], int(parts[1])
    chat_id = q.message.chat.id
    src = await get_src(c, chat_id, msg_id)
    if not src:
        await q.message.edit_text("⚠️ ফাইলটি পাওয়া যায়নি, আবার ফরওয়ার্ড করুন।")
        return
    base, ext = H.split_name(H.file_name_of(src))

    if act == "nm":
        pending[uid] = {"action": "ask_name", "msg_id": msg_id}
        await q.message.edit_text("✏️ নতুন নাম পাঠান (এক্সটেনশন ছাড়া)।\nবাতিল: /cancel")
    elif act == "ov":
        pending[uid] = {"action": "ask_name_overall", "msg_id": msg_id}
        await q.message.edit_text("✏️ নতুন নাম পাঠান (এক্সটেনশন ছাড়া)।\nবাতিল: /cancel")
    elif act == "fm":
        kb = fmt_kb(msg_id, "cf", ext)
        if not kb:
            await q.message.edit_text("⚠️ এই ধরনের ফাইলের জন্য কনভার্ট ফরমেট নেই। শুধু নাম বদলাতে পারবেন।")
            return
        await q.message.edit_text("🎞 ফরমেট বেছে নিন:", reply_markup=kb)
    elif act == "cf":
        await q.message.edit_text("⏳ প্রসেস হচ্ছে...")
        await run_job(c, chat_id, src, base, parts[2], q.message)
    elif act == "co":
        p = pending.get(uid)
        if not p or p.get("name") is None or p["msg_id"] != msg_id:
            await q.message.edit_text("⚠️ সেশন শেষ, আবার ফাইল ফরওয়ার্ড করুন।")
            return
        pending.pop(uid, None)
        await q.message.edit_text("⏳ প্রসেস হচ্ছে...")
        await run_job(c, chat_id, src, p["name"], parts[2], q.message)


async def health(_):
    return web.Response(text="OK")


async def main():
    load_settings()
    app = Client("filebot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
    media = (filters.document | filters.video | filters.audio | filters.voice |
             filters.animation | filters.video_note | filters.photo | filters.sticker)
    cmds = ["start", "mode", "help", "cancel", "gifdefault"]
    app.add_handler(MessageHandler(cmd_start, filters.private & filters.command(["start", "mode", "help"])))
    app.add_handler(MessageHandler(cmd_cancel, filters.private & filters.command("cancel")))
    app.add_handler(MessageHandler(cmd_gifdefault, filters.private & filters.command("gifdefault")))
    app.add_handler(MessageHandler(on_text, filters.private & filters.text & ~filters.command(cmds)))
    app.add_handler(MessageHandler(on_media, filters.private & media))
    app.add_handler(CallbackQueryHandler(on_cb))

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await app.start()
    log.info("Bot started")
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
