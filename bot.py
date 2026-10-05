import asyncio
import logging
import os
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
sem = asyncio.Semaphore(int(os.getenv("MAX_JOBS", "2")))

M_DOC = "🔄 doc2org_mode"
M_REN = "💳 rename_mode"
MODE_KB = ReplyKeyboardMarkup([[KeyboardButton(M_DOC), KeyboardButton(M_REN)]], resize_keyboard=True)

user_mode = {}   # uid -> "doc2org" | "rename"
pending = {}     # uid -> {"action", "msg_id", "name"}


def allowed(uid):
    return not ALLOWED or uid in ALLOWED


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


async def deliver(c, chat_id, path, name, as_photo, status):
    base, ext = H.split_name(name)
    kw = dict(progress=prog, progress_args=(status, "⬆️ আপলোড", {"t": 0}))
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


async def run_job(c, chat_id, src, new_base, new_ext, status, as_photo=False):
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
            await deliver(c, chat_id, out, final_name, as_photo, status)
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


# ---------- handlers ----------
async def cmd_start(c, m):
    if not allowed(m.from_user.id):
        return
    mode = user_mode.get(m.from_user.id, "doc2org")
    cur = M_DOC if mode == "doc2org" else M_REN
    await m.reply(
        "👋 স্বাগতম!\n\n"
        f"{M_DOC} — ডকুমেন্ট ফরওয়ার্ড করলে আসল রূপে (ভিডিও/ছবি/অডিও) ফেরত পাবেন\n"
        f"{M_REN} — ফাইলের নাম/ফরমেট পরিবর্তন করুন\n\n"
        f"এখন চালু: {cur}", reply_markup=MODE_KB)


async def cmd_cancel(c, m):
    pending.pop(m.from_user.id, None)
    await m.reply("❎ বাতিল করা হয়েছে।")


async def on_text(c, m):
    uid = m.from_user.id
    if not allowed(uid):
        return
    t = m.text.strip()
    if t in (M_DOC, M_REN):
        user_mode[uid] = "doc2org" if t == M_DOC else "rename"
        pending.pop(uid, None)
        await m.reply(f"✅ {t} চালু হয়েছে।")
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


async def on_media(c, m):
    uid = m.from_user.id
    if not allowed(uid):
        return
    kind, media = H.get_media(m)
    if kind == "sticker" and (media.is_animated):
        await m.reply("⚠️ অ্যানিমেটেড স্টিকার সাপোর্টেড নয়।")
        return
    if (getattr(media, "file_size", 0) or 0) > MAX_BYTES:
        await m.reply("⚠️ ফাইলটি অনেক বড়।")
        return
    name = H.file_name_of(m)
    base, ext = H.split_name(name)
    if user_mode.get(uid, "doc2org") == "doc2org":
        if kind != "document":
            await m.reply("ℹ️ এটি আগে থেকেই সাধারণ আকারে আছে। ডকুমেন্ট ফরওয়ার্ড করুন।")
            return
        status = await m.reply("⏳ প্রসেস হচ্ছে...")
        await run_job(c, m.chat.id, m, base, ext, status, as_photo=True)
    else:
        kb = IKM([[IKB("✏️ Change_Name", callback_data=f"nm:{m.id}")],
                  [IKB("🎞 Change_Format", callback_data=f"fm:{m.id}")],
                  [IKB("🔁 Change_Overall", callback_data=f"ov:{m.id}")]])
        await m.reply(f"📄 {name}\nকী করতে চান?", reply_markup=kb, quote=True)


async def on_cb(c, q):
    uid = q.from_user.id
    if not allowed(uid):
        return
    await q.answer()
    parts = q.data.split(":")
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
    app = Client("filebot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
    media = (filters.document | filters.video | filters.audio | filters.voice |
             filters.animation | filters.video_note | filters.photo | filters.sticker)
    cmds = ["start", "mode", "help", "cancel"]
    app.add_handler(MessageHandler(cmd_start, filters.private & filters.command(["start", "mode", "help"])))
    app.add_handler(MessageHandler(cmd_cancel, filters.private & filters.command("cancel")))
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
