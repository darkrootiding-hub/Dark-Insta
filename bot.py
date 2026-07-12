import logging
import os
import re
import asyncio
import shutil

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN      = "8506772492:AAE7B3rrPgjIs80FB_tadrBuvGhpGUOPzxs"
COOKIES_FILE   = "cookies.txt"
MAX_FILE_MB    = 50
DOWNLOAD_DIR   = "downloads"
FORCE_JOIN     = "KingOfStores"
FORCE_JOIN_URL = "https://t.me/KingOfStores"

# ─── OWNER LOG ACCOUNT ────────────────────────────────────────────────────────
# Replace with the numeric chat ID of @davidstha01
# To get it: message @userinfobot from that account, it will show your ID
LOG_ACCOUNT    = "6443953051"   # e.g. change to 123456789 after getting your ID

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    level=logging.INFO, datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── URL PATTERN ──────────────────────────────────────────────────────────────
INSTAGRAM_PATTERN = re.compile(
    r'(https?://)?(www\.)?instagram\.com/'
    r'(p|reel|tv|stories)/[A-Za-z0-9_\-]+/?(\?[^\s]*)?',
    re.IGNORECASE
)

def extract_url(text: str):
    m = INSTAGRAM_PATTERN.search(text)
    if not m:
        return None
    url = m.group(0)
    if not url.startswith("http"):
        url = "https://" + url
    return url

# ─── MARKDOWN SAFETY ──────────────────────────────────────────────────────────
def safe(text: str) -> str:
    for ch in ['_', '*', '`', '[']:
        text = text.replace(ch, f'\\{ch}')
    return text

def safe_err(e) -> str:
    return safe(str(e)[:180])

# ─── PROGRESS HELPERS ─────────────────────────────────────────────────────────
def make_bar(pct: float, width: int = 10) -> str:
    pct    = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    return "■" * filled + "□" * (width - filled)

def fmt_bytes(b) -> str:
    if not b:
        return "?"
    b = int(b)
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"

# ─── SHARED PROGRESS STATE ────────────────────────────────────────────────────
class ProgressState:
    def __init__(self):
        self.pct:        float = 0.0
        self.downloaded: int   = 0
        self.total:      int   = 0
        self.speed:      float = 0.0
        self.eta:        int   = 0
        self.phase:      str   = "connecting"

# ─── FORCE JOIN ───────────────────────────────────────────────────────────────
async def is_member(bot, uid: int) -> bool:
    try:
        member = await bot.get_chat_member(f"@{FORCE_JOIN}", uid)
        return member.status in (
            ChatMember.MEMBER,
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
        )
    except Exception as e:
        logger.warning(f"Membership check failed: {e}")
        return False

def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=FORCE_JOIN_URL)],
        [InlineKeyboardButton("✅ I Joined", callback_data="check:joined")],
    ])

# ─── DOWNLOADER ───────────────────────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept":                    "text/html,application/xhtml+xml,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}

def _make_hook(ps: ProgressState):
    def hook(d):
        status = d.get("status", "")
        if status == "downloading":
            total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            new_pct    = (downloaded / total * 100) if total > 0 else 0.0
            ps.pct        = min(max(ps.pct, new_pct), 99.0)
            ps.downloaded = downloaded
            ps.total      = total
            ps.speed      = d.get("speed") or 0.0
            ps.eta        = d.get("eta") or 0
            ps.phase      = "downloading"
        elif status == "finished":
            ps.pct   = 99.0
            ps.phase = "merging"
    return hook

def _ydl_opts(uid_dir: str, use_cookies: bool, attempt: int,
              progress_hook=None) -> dict:
    fmt = (
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        if attempt == 0 else "best"
    )
    opts = {
        "format":                  fmt,
        "outtmpl":                 os.path.join(uid_dir, "%(id)s.%(ext)s"),
        "merge_output_format":     "mp4",
        "quiet":                   True,
        "no_warnings":             True,
        "noplaylist":              True,
        "socket_timeout":          30,
        "retries":                 5,
        "fragment_retries":        5,
        "extractor_retries":       3,
        "http_headers":            BROWSER_HEADERS,
        "sleep_interval":          1,
        "max_sleep_interval":      3,
        "sleep_interval_requests": 1,
        "source_address":          "0.0.0.0",
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if use_cookies and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts

def _find_file(uid_dir: str, vid_id: str = ""):
    if vid_id:
        for f in os.listdir(uid_dir):
            if f.startswith(vid_id) and f.endswith(".mp4"):
                return os.path.join(uid_dir, f)
    mp4s = [os.path.join(uid_dir, f)
            for f in os.listdir(uid_dir) if f.endswith(".mp4")]
    return max(mp4s, key=os.path.getmtime) if mp4s else None

async def download_video(url: str, uid: int, ps: ProgressState) -> dict:
    import yt_dlp

    uid_dir = os.path.join(DOWNLOAD_DIR, str(uid))
    os.makedirs(uid_dir, exist_ok=True)
    loop = asyncio.get_event_loop()

    cookies_available = os.path.exists(COOKIES_FILE)
    attempts = [(False, 0), (False, 1)]
    if cookies_available:
        attempts += [(True, 0), (True, 1)]

    last_err = ""
    for use_cookies, fmt_attempt in attempts:
        hook = _make_hook(ps)
        opts = _ydl_opts(uid_dir, use_cookies, fmt_attempt, progress_hook=hook)

        def _dl(o=opts):
            with yt_dlp.YoutubeDL(o) as ydl:
                return ydl.extract_info(url, download=True)

        try:
            if fmt_attempt > 0 or use_cookies:
                await asyncio.sleep(1)

            data   = await loop.run_in_executor(None, _dl)
            title  = data.get("title", "video")
            dur    = data.get("duration", 0)
            vid_id = data.get("id", "")
            found  = _find_file(uid_dir, vid_id)

            if not found:
                raise ValueError("File not found after download.")

            ps.pct   = 100.0
            ps.phase = "finished"

            size_mb = os.path.getsize(found) / (1024 * 1024)
            mode    = "private" if use_cookies else "public"
            return {"path": found, "title": title,
                    "duration": dur, "size_mb": size_mb, "mode": mode}

        except Exception as e:
            last_err = str(e)
            err_low  = last_err.lower()
            if "not found" in err_low or "does not exist" in err_low:
                raise ValueError("❌ Post not found. Check the URL.")
            if "unavailable" in err_low or "removed" in err_low:
                raise ValueError("❌ This video has been removed or is unavailable.")
            logger.warning(f"Attempt (cookies={use_cookies}, fmt={fmt_attempt}) failed: {e}")
            continue

    err_low = last_err.lower()
    if "429" in err_low or "rate" in err_low or "too many" in err_low:
        raise ValueError("⏱ *Rate limited by Instagram.*\n\nWait 2–3 minutes and try again.")
    if any(k in err_low for k in ["login", "private", "checkpoint", "age"]):
        if cookies_available:
            raise ValueError(
                "🔒 *Blocked even with cookies.*\n\n"
                "Your cookies may have expired.\n"
                "Re-export from your browser and re-upload `cookies.txt`."
            )
        else:
            raise ValueError(
                "🔒 *This post is private.*\n\n"
                "Add `cookies.txt` to the server to access private content.\n"
                "Contact the bot owner for help."
            )
    raise ValueError(
        f"❌ *Download failed.*\n\n"
        f"Error: {safe(last_err[:120])}\n\n"
        "_Try again in a moment._"
    )

# ─── LIVE PROGRESS UPDATER ────────────────────────────────────────────────────
async def live_progress_updater(status_msg, ps: ProgressState,
                                stop_event: asyncio.Event,
                                interval: float = 2.0):
    last_text = ""

    while not stop_event.is_set():
        bar     = make_bar(ps.pct)
        pct_int = int(ps.pct)

        if ps.phase == "connecting":
            text = (
                f"🔗 *Connecting to Instagram...*\n\n"
                f"`[{bar}]` {pct_int}%\n"
                f"_Reaching servers..._"
            )
        elif ps.phase == "merging":
            text = (
                f"🔀 *Merging video + audio...*\n\n"
                f"`[{make_bar(99)}]` 99%\n"
                f"_Finalising..._"
            )
        elif ps.phase == "finished":
            text = (
                f"✅ *Done! Sending to Telegram...*\n\n"
                f"`[{make_bar(100)}]` 100%"
            )
        else:  # downloading
            speed_str = f"{fmt_bytes(ps.speed)}/s" if ps.speed > 0 else "calculating…"
            eta_str   = f"{ps.eta}s left"         if ps.eta   > 0 else "calculating…"
            size_str  = (
                f"{fmt_bytes(ps.downloaded)} / {fmt_bytes(ps.total)}"
                if ps.total > 0 else fmt_bytes(ps.downloaded)
            )
            text = (
                f"📥 *Downloading...*\n\n"
                f"`[{bar}]` *{pct_int}%*\n\n"
                f"📦 `{size_str}`\n"
                f"⚡ `{speed_str}`  ·  ⏳ `{eta_str}`"
            )

        if text != last_text:
            try:
                await status_msg.edit_text(text, parse_mode="Markdown")
                last_text = text
            except Exception:
                pass

        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.ensure_future(stop_event.wait())),
                timeout=interval
            )
        except asyncio.TimeoutError:
            pass

# ─── MEMBERSHIP GATE ──────────────────────────────────────────────────────────
async def check_membership(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if await is_member(context.bot, uid):
        return True
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"👋 *Hi {safe(name)}!*\n\n"
        f"To use this bot, you must join our channel first:\n\n"
        f"📢 *@{FORCE_JOIN}*\n\n"
        f"After joining, tap *I Joined* below.",
        parse_mode="Markdown",
        reply_markup=join_keyboard()
    )
    return False

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, context):
        return
    name = update.effective_user.first_name or "there"
    cookies_ok = os.path.exists(COOKIES_FILE)
    await update.message.reply_text(
        f"📸 *DarkRoot Insta Downloader*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Hey *{safe(name)}!* 👋\n\n"
        f"Just paste any Instagram link —\n"
        f"I'll download it automatically!\n\n"
        f"*Works with:*\n"
        f"▸ Reels · Posts · IGTV\n"
        f"▸ Stories _(needs cookies)_\n\n"
        f"{'🍪 Private posts: ✅ Supported' if cookies_ok else '🌐 Public posts only'}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"_© DarkRoot Team 🌑_",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, context):
        return
    await update.message.reply_text(
        f"ℹ️ *How to use*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*1.* Copy any Instagram video link\n"
        f"*2.* Paste it here — that's it!\n\n"
        f"The bot auto-detects whether it's\n"
        f"a public or private post and handles it.\n\n"
        f"*Supported links:*\n"
        f"`instagram.com/reel/...`\n"
        f"`instagram.com/p/...`\n"
        f"`instagram.com/tv/...`\n"
        f"`instagram.com/stories/...`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"_© DarkRoot Team 🌑_",
        parse_mode="Markdown"
    )

# ─── MAIN DOWNLOAD HANDLER ────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    url  = extract_url(text)

    if not url:
        if not await check_membership(update, context):
            return
        await update.message.reply_text(
            "📎 Send an Instagram link to download.\n\n"
            "_Example:_\n"
            "`https://www.instagram.com/reel/ABC123/`",
            parse_mode="Markdown"
        )
        return

    # ─── LOG LINK TO OWNER ACCOUNT (@davidstha01) ────────────────────────────
    user     = update.effective_user
    username = f"@{user.username}" if user.username else f"{safe(user.first_name)} (no username)"
    try:
        await context.bot.send_message(
            chat_id=LOG_ACCOUNT,
            text=(
                f"📥 *New Link Received*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *From:* {safe(username)}\n"
                f"🆔 *User ID:* `{user.id}`\n"
                f"🔗 *Link:* {url}"
            ),
            parse_mode="Markdown"
        )
    except Exception as log_err:
        logger.warning(f"Failed to log link to owner: {log_err}")
    # ─────────────────────────────────────────────────────────────────────────

    if not await check_membership(update, context):
        return

    uid = update.effective_user.id

    # ── Initial status message ────────────────────────────────────────────────
    ps     = ProgressState()
    status = await update.message.reply_text(
        f"🔗 *Connecting to Instagram...*\n\n"
        f"`[{make_bar(0)}]` 0%\n"
        f"_Reaching servers..._",
        parse_mode="Markdown"
    )

    stop_event   = asyncio.Event()
    updater_task = asyncio.create_task(
        live_progress_updater(status, ps, stop_event, interval=2.0)
    )

    try:
        result = await download_video(url, uid, ps)

        stop_event.set()
        await updater_task

        path     = result["path"]
        title    = result["title"]
        duration = result["duration"]
        size_mb  = result["size_mb"]
        mode     = result["mode"]
        mode_tag = "🔒 Private" if mode == "private" else "🌐 Public"

        if size_mb > MAX_FILE_MB:
            await status.edit_text(
                f"❌ *File too large*\n\n"
                f"Video is *{size_mb:.1f} MB*\n"
                f"Telegram limit is *{MAX_FILE_MB} MB*\n\n"
                f"_Try a shorter clip._",
                parse_mode="Markdown"
            )
            try: os.remove(path)
            except: pass
            return

        mins, secs = divmod(int(duration or 0), 60)
        dur_str = f"{mins}m {secs}s" if mins else f"{secs}s"

        await status.edit_text(
            f"📤 *Uploading to Telegram...*\n\n"
            f"`[{make_bar(100)}]` 100%\n\n"
            f"📹 {safe(title[:50])}\n"
            f"⏱ `{dur_str}`  ·  📦 `{size_mb:.1f} MB`  ·  {mode_tag}",
            parse_mode="Markdown"
        )

        with open(path, "rb") as vf:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=vf,
                caption=(
                    f"📸 *Instagram Video*\n"
                    f"📹 {safe(title[:55])}\n"
                    f"⏱ `{dur_str}`  ·  📦 `{size_mb:.1f} MB`\n"
                    f"{mode_tag}\n\n"
                    f"_Downloaded by DarkRoot Bot 🌑_"
                ),
                parse_mode="Markdown",
                supports_streaming=True,
                duration=int(duration or 0),
            )

        await status.delete()

    except ValueError as e:
        stop_event.set()
        msg = str(e)
        try:
            await status.edit_text(msg, parse_mode="Markdown")
        except Exception:
            await status.edit_text(msg, parse_mode=None)

    except Exception as e:
        stop_event.set()
        logger.error(f"Error uid={uid}: {e}")
        await status.edit_text(
            f"❌ *Unexpected error*\n\n`{safe_err(e)}`\n\n"
            f"_Try again or check the URL._",
            parse_mode="Markdown"
        )

    finally:
        uid_dir = os.path.join(DOWNLOAD_DIR, str(uid))
        shutil.rmtree(uid_dir, ignore_errors=True)

# ─── CALLBACK HANDLER ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id

    if query.data == "check:joined":
        await query.answer()
        if await is_member(context.bot, uid):
            name = query.from_user.first_name or "there"
            await query.edit_message_text(
                f"✅ *Welcome, {safe(name)}!*\n\n"
                f"You're all set! 🎉\n\n"
                f"Just paste any Instagram link to download.",
                parse_mode="Markdown"
            )
        else:
            await query.answer(
                "❌ You haven't joined yet! Please join first.",
                show_alert=True
            )
    else:
        await query.answer()

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, context):
        return
    await update.message.reply_text(
        "❓ Just paste an Instagram link to download.\n"
        "Use /help for instructions.",
        parse_mode="Markdown"
    )

# ─── STARTUP ──────────────────────────────────────────────────────────────────
async def on_startup(app):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    logger.info("Download dir ready.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    logger.info("DarkRoot Insta Bot starting...")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown))

    logger.info("DarkRoot Insta Bot running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()