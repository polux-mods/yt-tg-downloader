import asyncio
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    InlineQueryResultPhoto,
    InputMediaAudio,
    InputMediaVideo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

from yt_dlp import YoutubeDL


# =========================================================
# CONFIG & PATHS
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

INITIAL_ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

BASE_DIR = Path(__file__).resolve().parent
LOCAL_NODE_BIN = BASE_DIR / ".node" / "bin"
BGUTIL_DIR = BASE_DIR / "bgutil-ytdlp-pot-provider" / "server"
BGUTIL_MAIN = BGUTIL_DIR / "build" / "main.js"
BGUTIL_PORT = int(os.getenv("BGUTIL_PORT", "4416"))
BGUTIL_PROCESS = None

if LOCAL_NODE_BIN.is_dir():
    os.environ["PATH"] = str(LOCAL_NODE_BIN) + os.pathsep + os.environ.get("PATH", "")

MAX_FILE_SIZE = 49 * 1024 * 1024
COOKIES_FILE_PATH = BASE_DIR / "cookies.txt"
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()

DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# =========================================================
# TIMEZONE HELPER
# =========================================================
def get_kyiv_time() -> str:
    # Київський час (UTC+3 для простоти і уникнення сторонніх залежностей)
    tz = timezone(timedelta(hours=3))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


# =========================================================
# DATABASE SYSTEM (SQLite / PostgreSQL)
# =========================================================

def get_db_connection():
    if DATABASE_URL:
        import psycopg2
        db_url = DATABASE_URL
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(db_url), True
    else:
        conn = sqlite3.connect(BASE_DIR / "bot_database.db")
        return conn, False

def execute_query(query: str, params: tuple = (), fetchone=False, fetchall=False, commit=False):
    conn, is_postgres = get_db_connection()
    try:
        cursor = conn.cursor()
        sql = query
        if is_postgres:
            sql = sql.replace("?", "%s").replace("excluded.", "EXCLUDED.")
            if "AUTOINCREMENT" in sql:
                sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        cursor.execute(sql, params)
        
        res = None
        if fetchone: res = cursor.fetchone()
        elif fetchall: res = cursor.fetchall()
            
        if commit: conn.commit()
        return res
    finally:
        conn.close()

def sync_cookies_from_db():
    row = execute_query("SELECT value FROM settings WHERE key = ?", ("youtube_cookies",), fetchone=True)
    if row and row[0]: COOKIES_FILE_PATH.write_text(row[0], encoding="utf-8")
    elif YOUTUBE_COOKIES: COOKIES_FILE_PATH.write_text(YOUTUBE_COOKIES, encoding="utf-8")

def save_db_cookies(content: str):
    execute_query("""
        INSERT INTO settings (key, value) VALUES ('youtube_cookies', ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
    """, (content,), commit=True)
    COOKIES_FILE_PATH.write_text(content, encoding="utf-8")

def init_db():
    execute_query("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, lang TEXT)", commit=True)
    for col in ["username", "first_name", "joined_date", "last_active"]:
        try: execute_query(f"ALTER TABLE users ADD COLUMN {col} TEXT", commit=True)
        except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN downloads INTEGER DEFAULT 0", commit=True)
    except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT FALSE", commit=True)
    except: pass
    try: execute_query("ALTER TABLE users ADD COLUMN tos_accepted BOOLEAN DEFAULT FALSE", commit=True)
    except: pass

    execute_query("CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY)", commit=True)
    try: execute_query("ALTER TABLE admins ADD COLUMN added_by BIGINT", commit=True)
    except: pass
    try: execute_query("ALTER TABLE admins ADD COLUMN added_date TEXT", commit=True)
    except: pass
    try: execute_query("ALTER TABLE admins ADD COLUMN username TEXT", commit=True)
    except: pass

    execute_query("CREATE TABLE IF NOT EXISTS admin_history (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id BIGINT, action TEXT, action_date TEXT)", commit=True)
    
    execute_query("CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, title TEXT, invite_link TEXT)", commit=True)
    execute_query("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)", commit=True)
    execute_query("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, url TEXT, download_date TEXT)", commit=True)
    execute_query("CREATE TABLE IF NOT EXISTS inline_urls (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT)", commit=True)
    
    # Система зворотнього зв'язку
    execute_query("CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, status TEXT, created_at TEXT)", commit=True)
    execute_query("CREATE TABLE IF NOT EXISTS ticket_msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, sender TEXT, content TEXT, created_at TEXT)", commit=True)
    
    # Розсилки
    execute_query("CREATE TABLE IF NOT EXISTS broadcasts (id INTEGER PRIMARY KEY AUTOINCREMENT, b_type TEXT, b_val TEXT, chat_id BIGINT, msg_id INTEGER, status TEXT)", commit=True)

    for k, v in [('caption_bot_enabled', 'false'), ('caption_custom_text', '')]:
        if not execute_query("SELECT value FROM settings WHERE key = ?", (k,), fetchone=True):
            execute_query("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v), commit=True)

    if INITIAL_ADMIN_ID > 0:
        execute_query("""
            INSERT INTO admins (user_id, added_date, username) VALUES (?, ?, ?)
            ON CONFLICT (user_id) DO NOTHING
        """, (INITIAL_ADMIN_ID, get_kyiv_time(), "Owner"), commit=True)

    sync_cookies_from_db()


# --- User, Admin & System DB Helpers ---

def register_or_update_user(user_id: int, username: str, first_name: str, lang: str = "ua"):
    date_now = get_kyiv_time()
    uname = username or ""
    fname = first_name or ""
    row = execute_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not row:
        execute_query("""
            INSERT INTO users (user_id, lang, username, first_name, joined_date, downloads, is_banned, last_active, tos_accepted)
            VALUES (?, ?, ?, ?, ?, 0, FALSE, ?, FALSE)
        """, (user_id, lang, uname, fname, date_now, date_now), commit=True)
    else:
        execute_query("UPDATE users SET username = ?, first_name = ?, last_active = ? WHERE user_id = ?", 
                      (uname, fname, date_now, user_id), commit=True)

def has_accepted_tos(user_id: int) -> bool:
    row = execute_query("SELECT tos_accepted FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return row[0] if row else False

def accept_tos(user_id: int):
    execute_query("UPDATE users SET tos_accepted = TRUE WHERE user_id = ?", (user_id,), commit=True)

def get_user_info(user_id: int):
    return execute_query("SELECT user_id, lang, username, first_name, downloads, is_banned, joined_date, last_active FROM users WHERE user_id = ?", (user_id,), fetchone=True)

def is_user_banned(user_id: int) -> bool:
    row = execute_query("SELECT is_banned FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return row[0] if row else False

def set_user_ban(user_id: int, state: bool):
    execute_query("UPDATE users SET is_banned = ? WHERE user_id = ?", (state, user_id), commit=True)

def increment_downloads(user_id: int, url: str):
    execute_query("UPDATE users SET downloads = downloads + 1 WHERE user_id = ?", (user_id,), commit=True)
    execute_query("INSERT INTO history (user_id, url, download_date) VALUES (?, ?, ?)", (user_id, url, get_kyiv_time()), commit=True)

def get_user_history_all(user_id: int):
    return execute_query("SELECT url, download_date FROM history WHERE user_id = ? ORDER BY id DESC", (user_id,), fetchall=True)

def get_user_lang(user_id: int) -> str:
    row = execute_query("SELECT lang FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    return row[0] if row and row[0] else "ua"

def set_user_lang(user_id: int, lang: str):
    execute_query("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id), commit=True)

def is_admin(user_id: int) -> bool:
    if user_id == INITIAL_ADMIN_ID: return True
    return execute_query("SELECT user_id FROM admins WHERE user_id = ?", (user_id,), fetchone=True) is not None

def add_admin(user_id: int, added_by: int, username: str = None):
    execute_query("""
        INSERT INTO admins (user_id, added_by, added_date, username) VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id) DO NOTHING
    """, (user_id, added_by, get_kyiv_time(), username), commit=True)
    log_admin_action(added_by, f"Додано адміністратора {user_id}")

def remove_admin(user_id: int, removed_by: int):
    if user_id != INITIAL_ADMIN_ID:
        execute_query("DELETE FROM admins WHERE user_id = ?", (user_id,), commit=True)
        log_admin_action(removed_by, f"Видалено адміністратора {user_id}")

def get_all_admins_info():
    return execute_query("""
        SELECT a.user_id, a.added_by, a.added_date, COALESCE(u.first_name, a.username) as name
        FROM admins a LEFT JOIN users u ON a.user_id = u.user_id
    """, fetchall=True)

def get_admin_info(user_id: int):
    return execute_query("""
        SELECT a.user_id, a.added_by, a.added_date, COALESCE(u.first_name, a.username) as name, adder.first_name as adder_name
        FROM admins a 
        LEFT JOIN users u ON a.user_id = u.user_id
        LEFT JOIN users adder ON a.added_by = adder.user_id
        WHERE a.user_id = ?
    """, (user_id,), fetchone=True)

def log_admin_action(admin_id: int, action: str):
    execute_query("INSERT INTO admin_history (admin_id, action, action_date) VALUES (?, ?, ?)", (admin_id, action, get_kyiv_time()), commit=True)

def get_admin_history(admin_id: int):
    return execute_query("SELECT action, action_date FROM admin_history WHERE admin_id = ? ORDER BY id DESC LIMIT 20", (admin_id,), fetchall=True)

def get_users_page(page: int, limit: int = 10):
    offset = (page - 1) * limit
    rows = execute_query("""
        SELECT user_id, lang, username, first_name, downloads, is_banned, COALESCE(last_active, joined_date) 
        FROM users ORDER BY COALESCE(last_active, joined_date) DESC, user_id DESC LIMIT ? OFFSET ?
    """, (limit, offset), fetchall=True)
    total_rows = execute_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    total_pages = max(1, (total_rows + limit - 1) // limit)
    return rows, total_pages

def save_inline_url(url: str) -> int:
    execute_query("INSERT INTO inline_urls (url) VALUES (?)", (url,), commit=True)
    row = execute_query("SELECT MAX(id) FROM inline_urls", fetchone=True)
    return row[0] if row else 1

def get_inline_url(url_id: int) -> str:
    row = execute_query("SELECT url FROM inline_urls WHERE id = ?", (url_id,), fetchone=True)
    return row[0] if row else None

def get_file_caption(bot_username: str) -> str:
    bot_enabled = execute_query("SELECT value FROM settings WHERE key = 'caption_bot_enabled'", fetchone=True)
    custom_row = execute_query("SELECT value FROM settings WHERE key = 'caption_custom_text'", fetchone=True)
    
    lines = []
    if bot_enabled and bot_enabled[0] == "true" and bot_username:
        lines.append(f"@{bot_username}")
    if custom_row and custom_row[0]:
        if lines: lines.append("")
        lines.append(custom_row[0])
    return "\n".join(lines) if lines else None

# =========================================================
# LOCALIZATION (TEXTS)
# =========================================================

TEXTS = {
    "ua": {
        "btn_settings": "⚙️ Налаштування",
        "btn_profile": "👤 Профіль",
        "btn_admin": "🔑 Адмін меню",
        "btn_feedback": "✉️ Зворотній зв'язок",
        "start": "Привіт! 👋\nНадішли посилання на YouTube або YouTube Music.\nМожна відео, трек або плейлист.",
        "tos_text": "📜 **Умови користування ботом**\n\nКористуючись цим ботом, ви погоджуєтесь з правилами використання. Ми не зберігаємо ваші файли і бот працює виключно як інструмент для завантаження.\n\nБудь ласка, прийміть умови для продовження.",
        "tos_accept": "✅ Прийняти",
        "tos_decline": "❌ Відхилити",
        "tos_declined": "Ви відхилили умови користування. Використання бота неможливе.",
        
        "choose_format": "Обери формат:",
        "audio_btn": "🎵 Аудіо",
        "video_btn": "🎬 Відео",
        "download_audio": "🎵 Завантажити аудіо",
        
        "settings_main": "⚙️ **Налаштування**\nОберіть потрібний розділ:",
        "lang_menu_btn": "🌐 Мова",
        "settings_lang": "Оберіть бажану мову інтерфейсу:",
        "lang_set": "✅ Мову успішно змінено на Українську 🇺🇦",
        
        "profile_text": "👤 **Профіль користувача**\n\n**Ім'я:** {name}\n**ID:** `{id}`\n**Статус:** {status}\n**Останній онлайн (Київ):** {last_active}\n**Завантажень:** {downloads}",
        "status_user": "Користувач 👤",
        "status_admin": "Адміністратор 👑",
        
        "sub_required": "⚠️ **Для використання бота підпишіться на наші канали-спонсори:**",
        "check_sub_btn": "🔄 Перевірити підписку",
        "sub_success": "✅ Дякуємо за підписку! Надішліть посилання ще раз.",
        "sub_failed": "❌ Ви підписалися не на всі канали!",
        "invalid_url": "❌ Надішли коректне посилання YouTube або YouTube Music.",
        "banned_text": "❌ Ваш акаунт заблоковано.",
        
        "back_btn": "🔙 Назад",
        "cancel_btn": "❌ Скасувати",
        "close_btn": "❌ Закрити",
        
        "fetching_qualities": "🔎 Отримую список доступних якостей...",
        "no_qualities": "❌ Не вдалося отримати варіанти якості.",
        "choose_quality": "📹 **{title}**\n\nОберіть бажану якість:",
        "downloading_video": "⏳ Завантажую відео ({height}p)...",
        "downloading_audio": "⏳ Завантажую аудіо...",
        "file_too_large_video": "📦 **Файл перевищує 50 МБ** ({mb} МБ).\n\n🔗 [Натисніть сюди, щоб завантажити відео]({link})\n\n{caption}",
        "file_too_large_audio": "📦 **Аудіо перевищує 50 МБ** ({mb} МБ).\n\n🔗 [Натисніть сюди, щоб завантажити аудіо]({link})\n\n{caption}",
        "link_lost": "❌ Посилання втрачено. Надішліть його ще раз.",
        
        "admin_title": "🔑 **Панель адміністратора**",
        "admin_list_admins_btn": "👥 Адміни",
        "admin_add_admin_btn": "➕ Додати адміна",
        "admin_users_btn": "🔍 Пошук",
        "admin_all_users_btn": "👥 Учасники",
        "admin_caption_btn": "✍️ Підпис",
        "admin_broadcast_btn": "📢 Розсилка",
        "admin_tickets_btn": "📨 Модерація звернень",
        
        "admins_list_title": "👥 **Адміністратори:**",
        "admin_info": "👑 **Адмін:** {name}\n🆔 `{id}`\n📅 **Доданий:** {date}\n👤 **Ким:** {added_by}",
        "admin_del_btn": "🗑 Зняти адміна",
        "admin_hist_btn": "🕒 Історія дій",
        "admin_history_empty": "Історія пуста.",
        
        "user_info_admin": "👤 **Профіль:** {name}\n🆔 `{id}`\n🕒 **Онлайн:** {last_active}\n📥 **Завантажень:** {downloads}\n🚫 **Бан:** {banned}",
        "ban_btn": "🚫 Забанити",
        "unban_btn": "✅ Розбанити",
        "history_btn": "🕒 Історія завантажень",
        "make_admin_btn": "👑 Призначити адміном",
        "user_history_title": "🕒 **Всі завантаження ({id}):**\n",
        
        "broadcast_menu": "📢 **Конструктор розсилки**\n\nОберіть тип розсилки:",
        "bc_immediate": "⚡ Одразу",
        "bc_scheduled": "🕒 За розкладом",
        "bc_counter": "🔄 Кожен N-й запит",
        
        "feedback_menu": "✉️ **Зворотній зв'язок**\nОберіть дію:",
        "fb_new": "📝 Написати звернення",
        "fb_history": "🕒 Мої звернення",
        "fb_prompt": "Надішліть повідомлення для звернення (можна декілька). По завершенню натисніть кнопку відправити.",
        "fb_send_btn": "📤 Відправити звернення",
        "fb_sent": "✅ Звернення відправлено адміністраторам!",
        "fb_mod_empty": "🎉 Відкритих звернень немає.",
    },
    "en": {
        "btn_settings": "⚙️ Settings",
        "btn_profile": "👤 Profile",
        "btn_admin": "🔑 Admin Panel",
        "btn_feedback": "✉️ Feedback",
        "start": "Hello! 👋\nSend a YouTube or YouTube Music link.",
        "tos_text": "📜 **Terms of Service**\n\nPlease accept the terms to continue using the bot.",
        "tos_accept": "✅ Accept",
        "tos_decline": "❌ Decline",
        "tos_declined": "You declined the Terms of Service. You cannot use the bot.",
        
        "choose_format": "Choose format:",
        "audio_btn": "🎵 Audio",
        "video_btn": "🎬 Video",
        "download_audio": "🎵 Download audio",
        
        "settings_main": "⚙️ **Settings**",
        "lang_menu_btn": "🌐 Language",
        "settings_lang": "Select language:",
        "lang_set": "✅ Language set to English 🇬🇧",
        
        "profile_text": "👤 **Profile**\n\n**Name:** {name}\n**ID:** `{id}`\n**Status:** {status}\n**Last active:** {last_active}\n**Downloads:** {downloads}",
        "status_user": "User 👤",
        "status_admin": "Administrator 👑",
        
        "sub_required": "⚠️ **Please subscribe to our channels:**",
        "check_sub_btn": "🔄 Check subscription",
        "sub_success": "✅ Subscribed! Send link again.",
        "sub_failed": "❌ Not subscribed to all channels!",
        "invalid_url": "❌ Send a valid link.",
        "banned_text": "❌ Your account is banned.",
        
        "back_btn": "🔙 Back",
        "cancel_btn": "❌ Cancel",
        "close_btn": "❌ Close",
        
        "fetching_qualities": "🔎 Fetching qualities...",
        "no_qualities": "❌ No qualities available.",
        "choose_quality": "📹 **{title}**\n\nChoose quality:",
        "downloading_video": "⏳ Downloading video ({height}p)...",
        "downloading_audio": "⏳ Downloading audio...",
        "file_too_large_video": "📦 **File > 50 MB** ({mb} MB).\n\n🔗 [Download video]({link})\n\n{caption}",
        "file_too_large_audio": "📦 **Audio > 50 MB** ({mb} MB).\n\n🔗 [Download audio]({link})\n\n{caption}",
        "link_lost": "❌ Link lost.",
        
        "admin_title": "🔑 **Admin Panel**",
        "admin_list_admins_btn": "👥 Admins",
        "admin_add_admin_btn": "➕ Add Admin",
        "admin_users_btn": "🔍 Search",
        "admin_all_users_btn": "👥 Users",
        "admin_caption_btn": "✍️ Signature",
        "admin_broadcast_btn": "📢 Broadcast",
        "admin_tickets_btn": "📨 Tickets Mod",
        
        "admins_list_title": "👥 **Admins:**",
        "admin_info": "👑 **Admin:** {name}\n🆔 `{id}`\n📅 **Added:** {date}\n👤 **By:** {added_by}",
        "admin_del_btn": "🗑 Remove",
        "admin_hist_btn": "🕒 History",
        "admin_history_empty": "History empty.",
        
        "user_info_admin": "👤 **User:** {name}\n🆔 `{id}`\n🕒 **Active:** {last_active}\n📥 **Downloads:** {downloads}\n🚫 **Banned:** {banned}",
        "ban_btn": "🚫 Ban",
        "unban_btn": "✅ Unban",
        "history_btn": "🕒 History",
        "make_admin_btn": "👑 Make Admin",
        "user_history_title": "🕒 **All Downloads ({id}):**\n",
        
        "broadcast_menu": "📢 **Broadcast Builder**\n\nChoose type:",
        "bc_immediate": "⚡ Immediate",
        "bc_scheduled": "🕒 Scheduled",
        "bc_counter": "🔄 Every Nth request",
        
        "feedback_menu": "✉️ **Feedback**\nSelect:",
        "fb_new": "📝 New Ticket",
        "fb_history": "🕒 My Tickets",
        "fb_prompt": "Send your message(s) and click Send when done.",
        "fb_send_btn": "📤 Send Ticket",
        "fb_sent": "✅ Ticket sent!",
        "fb_mod_empty": "🎉 No open tickets.",
    }
}

def get_text(lang: str, key: str) -> str:
    l = lang if lang in TEXTS else "ua"
    return TEXTS[l].get(key, TEXTS["ua"].get(key, key))


# =========================================================
# KEYBOARDS & COMMANDS
# =========================================================

async def setup_bot_commands(app_bot):
    try: await app_bot.delete_my_commands()
    except: pass
    try: await app_bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    except: pass
    try: await app_bot.delete_my_commands(scope=BotCommandScopeDefault())
    except: pass

def get_main_keyboard(user_id: int, lang: str) -> ReplyKeyboardMarkup:
    keys = [
        [KeyboardButton(get_text(lang, "btn_settings")), KeyboardButton(get_text(lang, "btn_profile"))],
        [KeyboardButton(get_text(lang, "btn_feedback"))]
    ]
    if is_admin(user_id):
        keys.append([KeyboardButton(get_text(lang, "btn_admin"))])
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)

def get_cancel_inline(lang: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(get_text(lang, "cancel_btn"), callback_data=callback_data)]])


# =========================================================
# YT-DLP CORE LOGIC
# =========================================================

def youtube_options_base():
    options = {
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        },
        "extractor_args": {
            "youtube": {"player_client": ["android_vr", "mweb"]},
            "youtubepot-bgutilhttp": {"base_url": f"http://127.0.0.1:{BGUTIL_PORT}"},
        },
        "js_runtimes": {"node": {"path": str(LOCAL_NODE_BIN / "node")}},
    }
    if COOKIES_FILE_PATH.is_file(): options["cookiefile"] = str(COOKIES_FILE_PATH)
    return options

def start_bgutil_provider():
    global BGUTIL_PROCESS
    if not BGUTIL_MAIN.is_file() or not (LOCAL_NODE_BIN / "node").is_file(): return False
    BGUTIL_PROCESS = subprocess.Popen(
        [str(LOCAL_NODE_BIN / "node"), str(BGUTIL_MAIN), "--port", str(BGUTIL_PORT)],
        cwd=str(BGUTIL_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=os.environ.copy(),
    )
    import urllib.request
    deadline = time.time() + 15
    while time.time() < deadline:
        if BGUTIL_PROCESS.poll() is not None: return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{BGUTIL_PORT}/ping", timeout=1) as resp:
                if resp.status == 200: return True
        except Exception: time.sleep(0.25)
    return False

def stop_bgutil_provider():
    global BGUTIL_PROCESS
    if BGUTIL_PROCESS is not None and BGUTIL_PROCESS.poll() is None:
        BGUTIL_PROCESS.terminate()
        try: BGUTIL_PROCESS.wait(timeout=5)
        except: BGUTIL_PROCESS.kill()
    BGUTIL_PROCESS = None

def human_youtube_error(error: Exception) -> str:
    text = str(error)
    if "Sign in to confirm" in text: return "YouTube заблокував запит (anti-bot). Оновіть cookies."
    return text[:1000]

def extract_yt_id(url: str):
    match = re.search(r"(?:v=|/)([0-9A-Za-z_-]{11}).*", url)
    return match.group(1) if match else None

def is_youtube_url(url: str) -> bool:
    return bool(re.match(r"^https?://(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/", url, re.IGNORECASE))

def is_youtube_music_url(url: str) -> bool:
    return bool(re.match(r"^https?://music\.youtube\.com/", url, re.IGNORECASE))

def get_video_formats_info(url: str):
    options = youtube_options_base()
    options.update({"quiet": True, "no_warnings": True, "skip_download": True})
    
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        
    formats = info.get("formats", [])
    audio_bytes = 0
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            sz = f.get("filesize") or f.get("filesize_approx") or 0
            if sz > audio_bytes: audio_bytes = sz

    height_tiers = [1080, 720, 480, 360]
    available_qualities = []

    for h in height_tiers:
        video_bytes = 0
        found = False
        for f in formats:
            if f.get("vcodec") != "none" and f.get("height") == h:
                found = True
                sz = f.get("filesize") or f.get("filesize_approx") or 0
                if sz > video_bytes: video_bytes = sz

        if found:
            total_bytes = video_bytes + audio_bytes
            mb = round(total_bytes / (1024 * 1024), 1) if total_bytes > 0 else 0
            available_qualities.append({"height": h, "size_mb": mb})

    return available_qualities, info.get("title", "video")

def download_audio(url: str, workdir: str):
    output = str(Path(workdir) / "%(title).80s.%(ext)s")
    options = youtube_options_base()
    options.update({
        "format": "bestaudio/best",
        "outtmpl": output,
        "writethumbnail": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ],
    })
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            mp3_files = list(Path(workdir).glob("*.mp3"))
            if mp3_files: return str(mp3_files[0]), info
    except Exception:
        fallback_opts = youtube_options_base()
        fallback_opts.update({"format": "bestaudio/best", "outtmpl": output, "noplaylist": True, "quiet": True, "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}]})
        with YoutubeDL(fallback_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            mp3_files = list(Path(workdir).glob("*.mp3"))
            if mp3_files: return str(mp3_files[0]), info
    raise FileNotFoundError("MP3 file not found.")

def download_video_quality(url: str, workdir: str, height: int):
    output = str(Path(workdir) / "%(title).80s.%(ext)s")
    options = youtube_options_base()
    # Універсальний безпечний формат з фолбеком на best
    options.update({
        "format": f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
        "outtmpl": output,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
    })
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        video_files = [p for p in Path(workdir).iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm"}]
        if video_files: return str(video_files[0]), info
        raise FileNotFoundError("Video file not found.")


# =========================================================
# TELEGRAM HANDLERS
# =========================================================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_or_update_user(user.id, user.username or "", user.first_name or "")
    lang = get_user_lang(user.id)
    
    if not has_accepted_tos(user.id):
        kb = [[InlineKeyboardButton(get_text(lang, "tos_accept"), callback_data="tos_accept"),
               InlineKeyboardButton(get_text(lang, "tos_decline"), callback_data="tos_decline")]]
        await update.message.reply_text(get_text(lang, "tos_text"), reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return

    await update.message.reply_text(get_text(lang, "start"), reply_markup=get_main_keyboard(user.id, lang))

async def check_trigger_broadcast(bot, user_id: int):
    # Логіка N-го запиту
    uinfo = get_user_info(user_id)
    if not uinfo: return
    dls = uinfo[4]
    
    # Шукаємо активні розсилки типу counter
    bc = execute_query("SELECT id, b_val, chat_id, msg_id FROM broadcasts WHERE b_type='counter' AND status='active'", fetchall=True)
    for b in bc:
        target = int(b[1])
        if dls > 0 and dls % target == 0:
            try: await bot.copy_message(chat_id=user_id, from_chat_id=b[2], message_id=b[3])
            except: pass

async def master_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip() if update.message.text else ""
    register_or_update_user(user.id, user.username or "", user.first_name or "")
    lang = get_user_lang(user.id)
    
    if not has_accepted_tos(user.id):
        kb = [[InlineKeyboardButton(get_text(lang, "tos_accept"), callback_data="tos_accept"), InlineKeyboardButton(get_text(lang, "tos_decline"), callback_data="tos_decline")]]
        await update.message.reply_text(get_text(lang, "tos_text"), reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return

    if is_user_banned(user.id):
        await update.message.reply_text(get_text(lang, "banned_text"))
        return

    admin_state = context.user_data.get("admin_state")
    if admin_state:
        await handle_admin_inputs(update, context, text, admin_state, lang)
        return
        
    user_state = context.user_data.get("user_state")
    if user_state == "await_ticket_message":
        execute_query("INSERT INTO ticket_msgs (ticket_id, sender, content, created_at) VALUES (0, ?, ?, ?)", (str(user.id), update.message.text or "(Медіа файл)", get_kyiv_time()), commit=True)
        kb = [[InlineKeyboardButton(get_text(lang, "fb_send_btn"), callback_data="fb_send")], [InlineKeyboardButton(get_text(lang, "cancel_btn"), callback_data="close_menu")]]
        await update.message.reply_text("✅ Записано. Можете надіслати ще повідомлення або відправити.", reply_markup=InlineKeyboardMarkup(kb))
        return

    if text in [TEXTS["ua"]["btn_settings"], TEXTS["en"]["btn_settings"]]:
        keyboard = [[InlineKeyboardButton(get_text(lang, "lang_menu_btn"), callback_data="settings_lang")], [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]]
        await update.message.reply_text(get_text(lang, "settings_main"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
        
    if text in [TEXTS["ua"]["btn_profile"], TEXTS["en"]["btn_profile"]]:
        info = get_user_info(user.id)
        status = get_text(lang, "status_admin") if is_admin(user.id) else get_text(lang, "status_user")
        profile_msg = get_text(lang, "profile_text").format(name=info[3] or info[2] or f"ID: {user.id}", id=user.id, status=status, last_active=info[7] or info[6], downloads=info[4])
        await update.message.reply_text(profile_msg, parse_mode="Markdown")
        return
        
    if text in [TEXTS["ua"]["btn_feedback"], TEXTS["en"]["btn_feedback"]]:
        keyboard = [[InlineKeyboardButton(get_text(lang, "fb_new"), callback_data="fb_new_ticket")], [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]]
        await update.message.reply_text(get_text(lang, "feedback_menu"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if text in [TEXTS["ua"]["btn_admin"], TEXTS["en"]["btn_admin"]]:
        if not is_admin(user.id): return
        keyboard = [
            [InlineKeyboardButton(get_text(lang, "admin_list_admins_btn"), callback_data="admin_list_admins")],
            [InlineKeyboardButton(get_text(lang, "admin_users_btn"), callback_data="admin_users"), InlineKeyboardButton(get_text(lang, "admin_all_users_btn"), callback_data="users_page:1")],
            [InlineKeyboardButton(get_text(lang, "admin_tickets_btn"), callback_data="admin_tickets")],
            [InlineKeyboardButton(get_text(lang, "admin_caption_btn"), callback_data="admin_caption_menu"), InlineKeyboardButton(get_text(lang, "admin_broadcast_btn"), callback_data="admin_broadcast")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await update.message.reply_text(get_text(lang, "admin_title"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if text:
        if not is_youtube_url(text):
            await update.message.reply_text(get_text(lang, "invalid_url"))
            return
        context.user_data["url"] = text
        await send_format_selection(update.message, text, lang)

async def send_format_selection(message, url: str, lang: str, is_edit=False):
    kb = [[InlineKeyboardButton(get_text(lang, "download_audio"), callback_data="audio")]] if is_youtube_music_url(url) else [[InlineKeyboardButton(get_text(lang, "audio_btn"), callback_data="audio"), InlineKeyboardButton(get_text(lang, "video_btn"), callback_data="video")]]
    text = "🎵 YouTube Music" if is_youtube_music_url(url) else get_text(lang, "choose_format")
    if is_edit: await message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else: await message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    lang = get_user_lang(user_id)
    register_or_update_user(user_id, update.effective_user.username or "", update.effective_user.first_name or "")
    
    data = query.data
    
    if data == "tos_accept":
        accept_tos(user_id)
        await query.edit_message_text("✅")
        await update.effective_user.send_message(get_text(lang, "start"), reply_markup=get_main_keyboard(user_id, lang))
        return
    if data == "tos_decline":
        await query.edit_message_text(get_text(lang, "tos_declined"))
        return

    if is_user_banned(user_id):
        await query.edit_message_text(get_text(lang, "banned_text"))
        return

    if data in ["close_menu", "cancel_admin_action"]:
        context.user_data["admin_state"] = None
        context.user_data["user_state"] = None
        await query.message.delete()
        return
        
    if data == "cancel_to_admin_menu":
        context.user_data["admin_state"] = None
        data = "admin_menu"
        
    if data == "settings_lang":
        kb = [[InlineKeyboardButton("🇺🇦 Українська", callback_data="set_lang:ua"), InlineKeyboardButton("🇬🇧 English", callback_data="set_lang:en")], [InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="settings_main")]]
        await query.edit_message_text(get_text(lang, "settings_lang"), reply_markup=InlineKeyboardMarkup(kb))
        return
    if data == "settings_main":
        kb = [[InlineKeyboardButton(get_text(lang, "lang_menu_btn"), callback_data="settings_lang")], [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]]
        await query.edit_message_text(get_text(lang, "settings_main"), reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return
    if data.startswith("set_lang:"):
        set_user_lang(user_id, data.split(":")[1])
        await query.message.delete()
        await query.message.reply_text(get_text(data.split(":")[1], "lang_set"), reply_markup=get_main_keyboard(user_id, data.split(":")[1]))
        return
        
    # --- FEEDBACK ---
    if data == "fb_new_ticket":
        context.user_data["user_state"] = "await_ticket_message"
        await query.edit_message_text(get_text(lang, "fb_prompt"), reply_markup=get_cancel_inline(lang, "close_menu"))
        return
    if data == "fb_send":
        context.user_data["user_state"] = None
        execute_query("INSERT INTO tickets (user_id, status, created_at) VALUES (?, 'open', ?)", (user_id, get_kyiv_time()), commit=True)
        tid = execute_query("SELECT MAX(id) FROM tickets", fetchone=True)[0]
        execute_query("UPDATE ticket_msgs SET ticket_id = ? WHERE ticket_id = 0 AND sender = ?", (tid, str(user_id)), commit=True)
        await query.edit_message_text(get_text(lang, "fb_sent"))
        return

    # --- ADMIN CALLBACKS ---
    if data == "admin_menu" and is_admin(user_id):
        kb = [
            [InlineKeyboardButton(get_text(lang, "admin_list_admins_btn"), callback_data="admin_list_admins")],
            [InlineKeyboardButton(get_text(lang, "admin_users_btn"), callback_data="admin_users"), InlineKeyboardButton(get_text(lang, "admin_all_users_btn"), callback_data="users_page:1")],
            [InlineKeyboardButton(get_text(lang, "admin_tickets_btn"), callback_data="admin_tickets")],
            [InlineKeyboardButton(get_text(lang, "admin_caption_btn"), callback_data="admin_caption_menu"), InlineKeyboardButton(get_text(lang, "admin_broadcast_btn"), callback_data="admin_broadcast")],
            [InlineKeyboardButton(get_text(lang, "close_btn"), callback_data="close_menu")]
        ]
        await query.edit_message_text(get_text(lang, "admin_title"), reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return
        
    if data == "admin_list_admins" and is_admin(user_id):
        admins = get_all_admins_info()
        kb = [[InlineKeyboardButton(f"👑 {adm[3]}", callback_data=f"adm_view:{adm[0]}")] for adm in admins]
        kb.append([InlineKeyboardButton(get_text(lang, "admin_add_admin_btn"), callback_data="admin_add_admin")])
        kb.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        await query.edit_message_text(get_text(lang, "admins_list_title"), reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return
        
    if data.startswith("adm_view:") and is_admin(user_id):
        adm_id = int(data.split(":")[1])
        info = get_admin_info(adm_id)
        if not info: return
        adder_name = info[4] if info[4] else ("Система" if info[1] == 0 else "Unknown")
        text = get_text(lang, "admin_info").format(name=info[3], id=info[0], date=info[2], added_by=adder_name)
        kb = [[InlineKeyboardButton(get_text(lang, "admin_hist_btn"), callback_data=f"adm_hist:{adm_id}")]]
        if adm_id != INITIAL_ADMIN_ID: kb.append([InlineKeyboardButton(get_text(lang, "admin_del_btn"), callback_data=f"adm_del:{adm_id}")])
        kb.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_list_admins")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return
        
    if data.startswith("adm_hist:") and is_admin(user_id):
        adm_id = int(data.split(":")[1])
        hist = get_admin_history(adm_id)
        text = f"🕒 **Історія ({adm_id}):**\n\n"
        if not hist: text += get_text(lang, "admin_history_empty")
        for h in hist: text += f"• `{h[1]}`: {h[0]}\n"
        kb = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data=f"adm_view:{adm_id}")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return

    if data.startswith("adm_del:") and is_admin(user_id):
        remove_admin(int(data.split(":")[1]), user_id)
        await query.answer("✅", show_alert=True)
        await handle_callback(Update(update.update_id, callback_query=type('obj', (object,), {'data': 'admin_list_admins', 'answer': query.answer, 'edit_message_text': query.edit_message_text, 'message': query.message})()), context)
        return

    if data.startswith("users_page:") and is_admin(user_id):
        page = int(data.split(":")[1])
        rows, total = get_users_page(page, 10)
        kb = []
        for u in rows:
            name = f"🚫 {u[3] or u[2]}" if u[5] else (u[3] or u[2] or f"ID: {u[0]}")
            kb.append([InlineKeyboardButton(name, callback_data=f"usr_view:{u[0]}")])
        nav = []
        if page > 1: nav.append(InlineKeyboardButton("⬅️", callback_data=f"users_page:{page-1}"))
        if page < total: nav.append(InlineKeyboardButton("➡️", callback_data=f"users_page:{page+1}"))
        if nav: kb.append(nav)
        kb.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="admin_menu")])
        await query.edit_message_text(f"👥 Сторінка {page}/{total}", reply_markup=InlineKeyboardMarkup(kb))
        return
        
    if data.startswith("usr_view:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        info = get_user_info(target_id)
        text = get_text(lang, "user_info_admin").format(name=info[3] or info[2] or f"ID:{target_id}", id=info[0], last_active=info[7], downloads=info[4], banned="🔴 Так" if info[5] else "🟢 Ні")
        kb = [[InlineKeyboardButton(get_text(lang, "history_btn"), callback_data=f"usr_hist_full:{target_id}")]]
        if not info[5]: kb.append([InlineKeyboardButton(get_text(lang, "make_admin_btn"), callback_data=f"usr_mk_adm:{target_id}")])
        if info[5]: kb.append([InlineKeyboardButton(get_text(lang, "unban_btn"), callback_data=f"usr_unban:{target_id}")])
        else: kb.append([InlineKeyboardButton(get_text(lang, "ban_btn"), callback_data=f"usr_ban:{target_id}")])
        kb.append([InlineKeyboardButton(get_text(lang, "back_btn"), callback_data="users_page:1")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return
        
    if data.startswith("usr_mk_adm:") and is_admin(user_id):
        add_admin(int(data.split(":")[1]), user_id)
        await query.answer("✅", show_alert=True)
        return
        
    if data.startswith("usr_hist_full:") and is_admin(user_id):
        target_id = int(data.split(":")[1])
        hist = get_user_history_all(target_id)
        msg = get_text(lang, "user_history_title").format(id=target_id)
        if not hist: msg += "Пусто"
        for i, h in enumerate(hist[:30]): msg += f"{i+1}. `{h[1]}` - {h[0]}\n"
        if len(hist) > 30: msg += f"\n...і ще {len(hist)-30} записів."
        kb = [[InlineKeyboardButton(get_text(lang, "back_btn"), callback_data=f"usr_view:{target_id}")]]
        await query.edit_message_text(msg[:4000], reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown", disable_web_page_preview=True)
        return
        
    if data == "admin_broadcast" and is_admin(user_id):
        context.user_data["admin_state"] = "await_bc_msg"
        await query.edit_message_text("Надішліть повідомлення для розсилки:", reply_markup=get_cancel_inline(lang, "admin_menu"))
        return
    if data.startswith("bc_type:") and is_admin(user_id):
        btype = data.split(":")[1]
        source = context.user_data.get("broadcast_source")
        if btype == "imm":
            await query.edit_message_text("⏳ Розсилка...")
            success, failed = 0, 0
            users = execute_query("SELECT user_id FROM users WHERE is_banned = FALSE", fetchall=True)
            for u in users:
                try:
                    await context.bot.copy_message(chat_id=u[0], from_chat_id=source[0], message_id=source[1])
                    success += 1; await asyncio.sleep(0.04)
                except: failed += 1
            await query.edit_message_text(f"✅ Готово. Успіх: {success}, Помилок: {failed}")
        elif btype == "counter":
            context.user_data["admin_state"] = "await_bc_counter"
            await query.edit_message_text("Введіть число N (кожен N-й запит):")
        return

    # --- INLINE & DOWNLOADS ---
    if data.startswith("i_audio:") or data.startswith("i_video:"):
        is_video = data.startswith("i_video:")
        url_id = int(data.split(":")[1])
        url = get_inline_url(url_id)
        if not url:
            await query.answer(get_text(lang, "link_lost"), show_alert=True)
            return

        if is_video:
            await context.bot.edit_message_caption(inline_message_id=query.inline_message_id, caption=get_text(lang, "fetching_qualities"))
            qualities, title = await asyncio.to_thread(get_video_formats_info, url)
            if not qualities:
                await context.bot.edit_message_caption(inline_message_id=query.inline_message_id, caption=get_text(lang, "no_qualities"))
                return
            kb = [[InlineKeyboardButton(f"🎬 {q['height']}p (~{q['size_mb']} MB)", callback_data=f"i_vdl:{url_id}:{q['height']}")] for q in qualities]
            await context.bot.edit_message_reply_markup(inline_message_id=query.inline_message_id, reply_markup=InlineKeyboardMarkup(kb))
            return

        # Audio Inline
        await context.bot.edit_message_caption(inline_message_id=query.inline_message_id, caption=get_text(lang, "downloading_audio"))
        workdir = tempfile.mkdtemp(prefix="yt_tg_")
        try:
            filepath, info = await asyncio.to_thread(download_audio, url, workdir)
            file_size = os.path.getsize(filepath)
            increment_downloads(user_id, url)
            await check_trigger_broadcast(context.bot, user_id)
            caption = get_file_caption(context.bot.username) or ""

            if file_size <= MAX_FILE_SIZE:
                # Хитрість: завантажуємо файл в особисті повідомлення користувача, беремо file_id, видаляємо, і редагуємо інлайн повідомлення!
                with open(filepath, "rb") as f:
                    msg = await context.bot.send_audio(chat_id=user_id, audio=f, title=info.get("title")[:64], performer=info.get("uploader"), disable_notification=True)
                file_id = msg.audio.file_id
                await context.bot.edit_message_media(inline_message_id=query.inline_message_id, media=InputMediaAudio(media=file_id, caption=caption, parse_mode="Markdown"))
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                mb_size = round(file_size / (1024 * 1024), 1)
                text = get_text(lang, "file_too_large_audio").format(mb=mb_size, link=f"{PUBLIC_URL}/download/{safe_name}", caption=caption)
                await context.bot.edit_message_caption(inline_message_id=query.inline_message_id, caption=text, parse_mode="Markdown")
        except Exception as error:
            await context.bot.edit_message_caption(inline_message_id=query.inline_message_id, caption=f"❌ {human_youtube_error(error)}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return

    if data.startswith("i_vdl:"):
        _, url_id, height = data.split(":")
        url = get_inline_url(int(url_id))
        if not url: return
        await context.bot.edit_message_caption(inline_message_id=query.inline_message_id, caption=get_text(lang, "downloading_video").format(height=height))
        workdir = tempfile.mkdtemp(prefix="yt_tg_")
        try:
            filepath, info = await asyncio.to_thread(download_video_quality, url, workdir, int(height))
            file_size = os.path.getsize(filepath)
            increment_downloads(user_id, url)
            await check_trigger_broadcast(context.bot, user_id)
            caption = get_file_caption(context.bot.username) or ""

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    msg = await context.bot.send_video(chat_id=user_id, video=f, supports_streaming=True, disable_notification=True)
                file_id = msg.video.file_id
                await context.bot.edit_message_media(inline_message_id=query.inline_message_id, media=InputMediaVideo(media=file_id, caption=caption, parse_mode="Markdown"))
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                mb_size = round(file_size / (1024 * 1024), 1)
                text = get_text(lang, "file_too_large_video").format(mb=mb_size, link=f"{PUBLIC_URL}/download/{safe_name}", caption=caption)
                await context.bot.edit_message_caption(inline_message_id=query.inline_message_id, caption=text, parse_mode="Markdown")
        except Exception as error:
            await context.bot.edit_message_caption(inline_message_id=query.inline_message_id, caption=f"❌ {human_youtube_error(error)}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return

    # Звичайний режим бота
    if data == "audio" or data == "video":
        url = context.user_data.get("url")
        if not url: return
        
        if data == "video":
            status = await query.edit_message_text(get_text(lang, "fetching_qualities"))
            qualities, title = await asyncio.to_thread(get_video_formats_info, url)
            if not qualities:
                await status.edit_text(get_text(lang, "no_qualities"))
                return
            kb = [[InlineKeyboardButton(f"🎬 {q['height']}p (~{q['size_mb']} MB)", callback_data=f"vdl:{q['height']}")] for q in qualities]
            await status.edit_text(get_text(lang, "choose_quality").format(title=title[:60]), reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return
            
        status = await query.edit_message_text(get_text(lang, "downloading_audio"))
        workdir = tempfile.mkdtemp(prefix="yt_tg_")
        try:
            filepath, info = await asyncio.to_thread(download_audio, url, workdir)
            file_size = os.path.getsize(filepath)
            increment_downloads(user_id, url)
            await check_trigger_broadcast(context.bot, user_id)
            caption = get_file_caption(context.bot.username)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, title=info.get("title")[:64], performer=info.get("uploader"), caption=caption, parse_mode="Markdown")
                await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                text = get_text(lang, "file_too_large_audio").format(mb=round(file_size/(1024*1024),1), link=f"{PUBLIC_URL}/download/{safe_name}", caption=caption or "")
                await status.edit_text(text, parse_mode="Markdown")
        except Exception as error:
            await status.edit_text(f"❌ {human_youtube_error(error)}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    if data.startswith("vdl:"):
        height = int(data.split(":")[1])
        url = context.user_data.get("url")
        status = await query.edit_message_text(get_text(lang, "downloading_video").format(height=height))
        workdir = tempfile.mkdtemp(prefix="yt_tg_")
        try:
            filepath, info = await asyncio.to_thread(download_video_quality, url, workdir, height)
            file_size = os.path.getsize(filepath)
            increment_downloads(user_id, url)
            await check_trigger_broadcast(context.bot, user_id)
            caption = get_file_caption(context.bot.username)

            if file_size <= MAX_FILE_SIZE:
                with open(filepath, "rb") as f:
                    await context.bot.send_video(chat_id=query.message.chat_id, video=f, supports_streaming=True, caption=caption, parse_mode="Markdown")
                await status.delete()
            else:
                safe_name = f"{int(time.time())}_{Path(filepath).name}"
                shutil.move(filepath, DOWNLOADS_DIR / safe_name)
                text = get_text(lang, "file_too_large_video").format(mb=round(file_size/(1024*1024),1), link=f"{PUBLIC_URL}/download/{safe_name}", caption=caption or "")
                await status.edit_text(text, parse_mode="Markdown")
        except Exception as error:
            await status.edit_text(f"❌ {human_youtube_error(error)}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_or_update_user(user.id, user.username or "", user.first_name or "")
    query = update.inline_query.query.strip()

    if not query or not is_youtube_url(query):
        return

    url_id = save_inline_url(query)
    yt_id = extract_yt_id(query)
    thumb = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg" if yt_id else "https://via.placeholder.com/640x360.png?text=YouTube"

    results = [
        InlineQueryResultPhoto(
            id=str(url_id),
            photo_url=thumb,
            thumb_url=thumb,
            title="📥 Завантажити медіа",
            caption=f"🔗 **Посилання:** {query}\n\nОберіть формат:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎵 Аудіо", callback_data=f"i_audio:{url_id}"), InlineKeyboardButton("🎬 Відео", callback_data=f"i_video:{url_id}")]])
        )
    ]
    await update.inline_query.answer(results, cache_time=1)

async def handle_admin_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, state: str, lang: str):
    user_id = update.effective_user.id
    if state == "await_bc_msg":
        context.user_data["broadcast_source"] = (update.effective_chat.id, update.message.message_id)
        kb = [[InlineKeyboardButton(get_text(lang, "bc_immediate"), callback_data="bc_type:imm")],
              [InlineKeyboardButton(get_text(lang, "bc_counter"), callback_data="bc_type:counter")]]
        await update.message.reply_text(get_text(lang, "broadcast_menu"), reply_markup=InlineKeyboardMarkup(kb))
    elif state == "await_bc_counter":
        if text.isdigit():
            execute_query("INSERT INTO broadcasts (b_type, b_val, chat_id, msg_id, status) VALUES ('counter', ?, ?, ?, 'active')", (text, context.user_data["broadcast_source"][0], context.user_data["broadcast_source"][1]), commit=True)
            context.user_data["admin_state"] = None
            await update.message.reply_text(f"✅ Розсилку встановлено на кожен {text}-й запит користувача.")
        else:
            await update.message.reply_text("❌ Введіть число.")

# =========================================================
# APPLICATION SETUP
# =========================================================

telegram_app = Application.builder().token(TOKEN).updater(None).build()
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(InlineQueryHandler(inline_query_handler))
telegram_app.add_handler(MessageHandler(filters.TEXT, master_text_handler))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_bgutil_provider()
    await telegram_app.initialize()
    await telegram_app.start()
    await setup_bot_commands(telegram_app.bot)
    await telegram_app.bot.set_webhook(url=f"{PUBLIC_URL}/telegram/webhook", secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET else None, allowed_updates=Update.ALL_TYPES)
    yield
    await telegram_app.stop()
    await telegram_app.shutdown()
    stop_bgutil_provider()

app = FastAPI(lifespan=lifespan)

@app.get("/download/{filename}")
async def get_download_file(filename: str):
    file_path = DOWNLOADS_DIR / filename
    if file_path.is_file(): return FileResponse(file_path, media_type="application/octet-stream", filename=filename)
    raise HTTPException(status_code=404)

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401)
    await telegram_app.update_queue.put(Update.de_json(await request.json(), telegram_app.bot))
    return PlainTextResponse("OK")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
