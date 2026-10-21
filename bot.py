"""Telegram media downloader powered by yt-dlp and spotDL."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from health_server import start_health_server

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
MAX_UPLOAD_MB = max(1, min(int(os.getenv("MAX_UPLOAD_MB", "49")), 49))
MAX_PLAYLIST_ITEMS = max(1, min(int(os.getenv("MAX_PLAYLIST_ITEMS", "10")), 100))
MAX_DURATION_SECONDS = max(60, int(os.getenv("MAX_DURATION_SECONDS", "7200")))
MAX_CONCURRENT_DOWNLOADS = max(1, min(int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")), 8))
SPOTIFY_AUDIO_FORMAT = os.getenv("SPOTIFY_AUDIO_FORMAT", "mp3").lower()
SPOTIFY_BITRATE = os.getenv("SPOTIFY_BITRATE", "192k")
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip() or None
JOB_TTL_SECONDS = 20 * 60
MAX_JOBS = 500

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("universal-media-bot")

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
SPOTIFY_RE = re.compile(r"https?://open\.spotify\.com/(track|album|playlist)/", re.I)
DOWNLOADS = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


@dataclass
class Job:
    url: str
    title: str
    caption: str
    formats: list[dict[str, Any]]
    owner_id: int
    created_at: float


JOBS: dict[str, Job] = {}


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and (not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS))


def validate_url(url: str) -> str:
    """Accept public HTTP(S) URLs only; reject local/private hosts before yt-dlp sees them."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("فقط لینک‌های عمومی http یا https پشتیبانی می‌شوند.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("لینک‌های شبکهٔ خصوصی پشتیبانی نمی‌شوند.")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        except (OSError, ValueError):
            raise ValueError("دامنهٔ لینک پیدا نشد یا در دسترس نیست.") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("لینک‌های شبکهٔ خصوصی پشتیبانی نمی‌شوند.")
    return url


def ydl_base(download_dir: str | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "noprogress": True,
        "max_filesize": MAX_UPLOAD_MB * 1024 * 1024,
        "match_filter": lambda info, incomplete: (
            f"ویدیو از محدودیت {MAX_DURATION_SECONDS // 60} دقیقه طولانی‌تر است"
            if info.get("duration") and info["duration"] > MAX_DURATION_SECONDS
            else None
        ),
    }
    if download_dir:
        opts["outtmpl"] = str(Path(download_dir) / "%(title).100s-%(id)s.%(ext)s")
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE
    return opts


def make_caption(info: dict[str, Any]) -> str:
    title = str(info.get("title") or "بدون عنوان")
    uploader = str(info.get("uploader") or info.get("channel") or info.get("artist") or "")
    description = str(info.get("description") or "").strip()
    original = str(info.get("webpage_url") or info.get("original_url") or "").strip()
    parts = [title]
    if uploader:
        parts.append(f"از {uploader}")
    if description:
        short = description[:500].strip()
        parts.extend(["", short + ("…" if len(description) > 500 else "")])
    if original:
        parts.extend(["", original])
    return "\n".join(parts)[:1024]


def list_formats(url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with yt_dlp.YoutubeDL(ydl_base()) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("از این لینک رسانه‌ای پیدا نشد.")
    if info.get("entries"):
        info = next((entry for entry in info["entries"] if entry), None)
    if not info:
        raise RuntimeError("از این لینک رسانه‌ای پیدا نشد.")

    heights = sorted({int(f["height"]) for f in (info.get("formats") or []) if f.get("height") and f.get("vcodec") != "none"}, reverse=True)
    options: list[dict[str, Any]] = []
    for height in heights[:6]:
        options.append({"id": f"height:{height}", "label": f"حداکثر {height}p"})
    options.extend([
        {"id": "best", "label": "بهترین کیفیت ویدیو"},
        {"id": "audio", "label": "فقط صدا (MP3)"},
        {"id": "caption", "label": "فقط کپشن"},
    ])
    return info, options


def download_media(
    url: str,
    fmt: str,
    target: str,
    progress_hook: Any | None = None,
) -> tuple[Path, dict[str, Any]]:
    opts = ydl_base(target)
    opts.update({"noplaylist": True, "merge_output_format": "mp4"})
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if fmt == "audio":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        })
    elif fmt == "best":
        opts["format"] = "bestvideo*+bestaudio/best"
    elif fmt.startswith("height:") and fmt[7:].isdigit():
        height = int(fmt[7:])
        opts["format"] = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best[height<={height}]"
    else:
        raise ValueError("گزینهٔ کیفیت معتبر نیست؛ لینک را دوباره بفرست.")

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("entries"):
            info = next((entry for entry in info["entries"] if entry), info)

    files = [p for p in Path(target).iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("فایل خروجی ساخته نشد.")
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    final = next((p for p in files if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".mov"}), files[0])
    if final.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        raise RuntimeError(f"فایل از سقف {MAX_UPLOAD_MB} مگابایت تلگرام بزرگ‌تر شد؛ کیفیت پایین‌تر را انتخاب کن.")
    return final, info


def download_spotify(url: str, target: str) -> list[Path]:
    # spotDL resolves track metadata to audio from supported third-party sources;
    # Spotify's protected audio stream itself is not downloaded.
    output = str(Path(target) / "{artists} - {title}.{output-ext}")
    catalog = Path(target) / "songs.spotdl"
    save = subprocess.run(
        ["spotdl", "save", url, "--save-file", str(catalog)],
        capture_output=True, text=True, timeout=600, check=False,
    )
    if save.returncode != 0:
        message = (save.stderr or save.stdout or "خطای دریافت اطلاعات Spotify").strip()
        raise RuntimeError(message[-1200:])
    try:
        songs = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("اطلاعات آهنگ‌های Spotify قابل خواندن نیست.") from exc
    if not isinstance(songs, list):
        raise RuntimeError("قالب فهرست Spotify معتبر نیست.")
    catalog.write_text(json.dumps(songs[:MAX_PLAYLIST_ITEMS], ensure_ascii=False), encoding="utf-8")

    command = [
        "spotdl", "download", str(catalog),
        "--output", output,
        "--format", SPOTIFY_AUDIO_FORMAT,
        "--bitrate", SPOTIFY_BITRATE,
        "--threads", "2",
        "--max-retries", "2",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=3600, check=False)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "خطای spotDL").strip()
        raise RuntimeError(message[-1200:])
    files = sorted(Path(target).glob(f"*.{SPOTIFY_AUDIO_FORMAT}"), key=lambda path: path.name.lower())
    return files[:MAX_PLAYLIST_ITEMS]


def prune_jobs() -> None:
    now = time.time()
    for key, job in list(JOBS.items()):
        if now - job.created_at > JOB_TTL_SECONDS:
            JOBS.pop(key, None)
    while len(JOBS) > MAX_JOBS:
        oldest = min(JOBS, key=lambda key: JOBS[key].created_at)
        JOBS.pop(oldest, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.message:
        return
    await update.message.reply_text(
        "سلام! لینک عمومی ویدیو یا صوت را بفرست. بات از yt-dlp استفاده می‌کند و هزاران سایت را پوشش می‌دهد؛ "
        "کارکرد هر سایت به تغییرات همان سرویس وابسته است.\n\n"
        "• کیفیت‌های موجود، بهترین کیفیت یا MP3\n"
        "• کپشن و اطلاعات رسانه\n"
        "• لینک Spotify برای تطبیق آهنگ و دریافت نسخهٔ قابل‌دسترس از منابع پشتیبانی‌شده\n\n"
        "برای راهنما /help و برای شناسه /id را بفرست. فقط محتوایی را بگیر که حق دریافتش را داری."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.message:
        return
    await update.message.reply_text(
        "لینک یک رسانهٔ عمومی را همین‌جا بفرست. لینک‌های YouTube، Instagram، TikTok، X، Facebook، "
        "Reddit، Vimeo، SoundCloud و سایت‌های دیگر را به شرط پشتیبانی yt-dlp امتحان می‌کنم.\n\n"
        "محدودیت‌ها: لینک خصوصی/نیازمند ورود ممکن است کار نکند؛ برای بعضی سایت‌ها تغییرات اخیر یا محدودیت منطقه‌ای مشکل‌ساز است؛ "
        "محدودیت حجم و زمان در تنظیمات سرور اعمال می‌شود. لینک Spotify فایل اصلی DRM را نمی‌دهد و spotDL آهنگ مشابه را از منابع دیگر پیدا می‌کند."
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        await update.message.reply_text(f"آیدی عددی تلگرام شما: `{update.effective_user.id}`", parse_mode=ParseMode.MARKDOWN)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.message or not update.message.text:
        return
    match = URL_RE.search(update.message.text)
    if not match:
        await update.message.reply_text("یک لینک عمومی معتبر بفرست.")
        return
    url = match.group(0).rstrip(".,);]")
    status = await update.message.reply_text("دارم لینک را بررسی می‌کنم…")
    try:
        await asyncio.to_thread(validate_url, url)
    except ValueError as exc:
        await status.edit_text(str(exc))
        return

    if SPOTIFY_RE.search(url):
        await status.edit_text("لینک Spotify شناسایی شد؛ دارم ترک‌ها را از منابع در دسترس پیدا می‌کنم…")
        tmp = tempfile.mkdtemp(prefix="spotify_")
        try:
            async with DOWNLOADS:
                files = await asyncio.to_thread(download_spotify, url, tmp)
            if not files:
                raise RuntimeError("هیچ آهنگی پیدا نشد.")
            await status.edit_text(f"{len(files)} فایل آماده شد؛ در حال ارسال…")
            for path in files:
                if path.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                    await update.message.reply_text(f"فایل {path.name} از محدودیت {MAX_UPLOAD_MB} مگابایت بزرگ‌تر است.")
                    continue
                with path.open("rb") as media:
                    await update.message.reply_audio(audio=media, title=path.stem[:64], read_timeout=180, write_timeout=180)
            await status.delete()
        except Exception as exc:
            log.exception("Spotify failure")
            await status.edit_text(f"دانلود انجام نشد:\n{html.escape(str(exc))[:2000]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return

    try:
        async with DOWNLOADS:
            info, formats = await asyncio.to_thread(list_formats, url)
        job_id = uuid.uuid4().hex[:10]
        user_id = update.effective_user.id if update.effective_user else 0
        JOBS[job_id] = Job(url=url, title=str(info.get("title") or "Media"), caption=make_caption(info), formats=formats, owner_id=user_id, created_at=time.time())
        prune_jobs()
        keyboard = [
            [InlineKeyboardButton(item["label"], callback_data=f"cap|{job_id}")] if item["id"] == "caption"
            else [InlineKeyboardButton(item["label"], callback_data=f"dl|{job_id}|{item['id']}")]
            for item in formats
        ]
        preview = html.escape(make_caption(info)[:700])
        await status.edit_text(
            f"<b>{html.escape(str(info.get('title') or 'Media'))}</b>\n\n<blockquote>{preview}</blockquote>\n\nکیفیت را انتخاب کن:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log.exception("Probe failure")
        await status.edit_text(f"نتوانستم لینک را بررسی کنم:\n{html.escape(str(exc))[:2000]}")


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not allowed(update):
        return
    parts = (query.data or "").split("|", 2)
    if len(parts) < 2:
        return
    action, job_id = parts[:2]
    job = JOBS.get(job_id)
    if not job or time.time() - job.created_at > JOB_TTL_SECONDS:
        JOBS.pop(job_id, None)
        await query.edit_message_text("این انتخاب منقضی شده؛ لینک را دوباره بفرست.")
        return
    if not update.effective_user or update.effective_user.id != job.owner_id:
        await query.message.reply_text("این انتخاب برای کاربر دیگری است.")
        return
    if action == "cap":
        await query.message.reply_text(job.caption, disable_web_page_preview=True)
        return
    if action != "dl" or len(parts) != 3:
        return
    fmt = parts[2]
    if fmt not in {item["id"] for item in job.formats}:
        await query.edit_message_text("گزینهٔ کیفیت معتبر نیست؛ لینک را دوباره بفرست.")
        return

    await query.edit_message_text("دانلود شروع شد…")
    tmp = tempfile.mkdtemp(prefix="media_")
    try:
        loop = asyncio.get_running_loop()
        last_progress_update = [0.0]

        def report_progress(data: dict[str, Any]) -> None:
            if data.get("status") != "downloading":
                return
            now = time.monotonic()
            if now - last_progress_update[0] < 5:
                return
            last_progress_update[0] = now
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            downloaded_mb = downloaded / 1024 / 1024
            if total:
                percent = min(100, int(downloaded * 100 / total))
                message = f"در حال دانلود… {percent}% · {downloaded_mb:.1f}/{total / 1024 / 1024:.1f} مگابایت"
            else:
                message = f"در حال دانلود… {downloaded_mb:.1f} مگابایت دریافت شده"

            def schedule_edit() -> None:
                task = asyncio.create_task(query.edit_message_text(message))

                def ignore_edit_error(completed: asyncio.Task[Any]) -> None:
                    if not completed.cancelled():
                        try:
                            completed.result()
                        except Exception:
                            pass

                task.add_done_callback(ignore_edit_error)

            loop.call_soon_threadsafe(schedule_edit)

        async with DOWNLOADS:
            path, info = await asyncio.to_thread(download_media, job.url, fmt, tmp, report_progress)
        caption = make_caption(info)
        with path.open("rb") as media:
            if path.suffix.lower() in {".mp3", ".m4a"}:
                await query.message.reply_audio(audio=media, caption=caption, read_timeout=180, write_timeout=180)
            else:
                await query.message.reply_document(document=media, caption=caption, filename=path.name, read_timeout=180, write_timeout=180)
        JOBS.pop(job_id, None)
        await query.delete_message()
    except Exception as exc:
        log.exception("Download failure")
        await query.edit_message_text(f"دانلود انجام نشد:\n{html.escape(str(exc))[:2000]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN را در متغیرهای محیطی یا فایل .env تنظیم کن")
    start_health_server()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CallbackQueryHandler(callback, pattern=r"^(dl|cap)\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
