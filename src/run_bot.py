import os
import asyncio
import logging
import configparser
from typing import Dict, Any, Optional

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from motor.motor_asyncio import AsyncIOMotorClient
from bson import BSON

from get_creds import get_creds
from kosokolovsky_telegram_bot import MyBot
from main import doneOrNot

# ----------------- LOGGING -----------------
log_dir = os.path.join(os.getcwd(), "logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(log_dir, "bot_stdout.log")),
        logging.StreamHandler()
    ]
)

# ----------------- CREDS / CONFIG -----------------
USERS, TOKEN, API_URL = get_creds()

# Read creds.ini from the working directory (CI writes it here)
# creds_path = os.path.join(os.getcwd(), "creds.ini")
creds_path = "/home/ubuntu/tutor_bot/creds.ini"
config = configparser.ConfigParser()
config.read(creds_path)

dbname = config["MAIN"]["dbname"]
username = config["MAIN"]["username"]
password = config["MAIN"]["password"]
rds_endpoint = config["MAIN"]["rds_endpoint"]

# ----------------- MONGO -----------------
# Configure MongoDB connection (replica set is required for change streams)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/?replicaSet=rs0")
DB_NAME = os.getenv("MONGO_DB", "log_db")
ANSWERS_COLL = os.getenv("MONGO_COLL", "logs")

# Persist resume token to avoid missing events across restarts
RESUME_FILE = os.path.join(os.path.dirname(__file__), "cache", "mongo_resume_token.bson")

# Avoid excessive recomputes when multiple events arrive in a short burst
DEBOUNCE_SECONDS = 3.0
_debounce: Dict[str, float] = {}

# ----------------- HELPERS -----------------
def format_missing_tasks_markdown(missing_tasks: Dict[int, Any]) -> str:
    """Format result of doneOrNot into a Markdown message."""
    if not missing_tasks:
        return "✅ All is done. Enjoy the moment!"
    message = "*📌 ToDo:*\n\n"
    for task, nums in missing_tasks.items():
        nums_str = ", ".join(map(str, nums))
        message += f"• *Task {task}:* \n\t`{nums_str}`\n\n"
    return message

def chat_id_to_student(chat_id: int) -> Optional[str]:
    """Map Telegram chat_id to student_name using USERS mapping."""
    for name, cid in USERS.items():
        if str(cid) == str(chat_id):
            return name
    return None

def _save_resume_token(token: Dict[str, Any]) -> None:
    """Persist the change stream resume token to disk (binary BSON)."""
    if not token:
        return
    os.makedirs(os.path.dirname(RESUME_FILE), exist_ok=True)
    with open(RESUME_FILE, "wb") as f:
        f.write(BSON.encode(token))

def _load_resume_token() -> Optional[Dict[str, Any]]:
    """Load resume token from disk (if present)."""
    try:
        with open(RESUME_FILE, "rb") as f:
            return BSON(f.read()).decode()
    except FileNotFoundError:
        return None
    except Exception:
        logging.exception("Failed to load resume token")
        return None

# ----------------- CACHE KEYS -----------------
# application.bot_data will hold two views:
#   progress/<student_name>  -> raw dict from doneOrNot
#   progress_msg/<chat_id>   -> preformatted Markdown message
PROGRESS_KEY = "progress/{student}"
PROGRESS_MSG_KEY = "progress_msg/{chat_id}"

async def recompute_student(application, student_name: str) -> None:
    """
    Recompute a student's progress and update the cache.
    No messages are sent here; only cache gets updated.
    """
    try:
        # doneOrNot is blocking → run it in a thread pool
        nums = await asyncio.to_thread(doneOrNot, student_name=student_name)
        application.bot_data[PROGRESS_KEY.format(student=student_name)] = nums

        chat_id = USERS.get(student_name)
        if chat_id:
            msg = format_missing_tasks_markdown(nums)
            application.bot_data[PROGRESS_MSG_KEY.format(chat_id=chat_id)] = msg

        logging.info("Cache updated for %s", student_name)
    except Exception:
        logging.exception("Failed to recompute for %s", student_name)

# ----------------- MONGO WATCHER -----------------
async def watch_answers(application) -> None:
    """
    Listen to MongoDB change streams on the answers collection.
    On insert/update/replace → recompute cache for the affected student.
    """
    client = AsyncIOMotorClient(MONGO_URI)
    coll = client[DB_NAME][ANSWERS_COLL]

    pipeline = [
        {"$match": {"operationType": {"$in": ["insert", "update", "replace"]}}}
        # If needed, add more filters here
    ]

    resume_token = _load_resume_token()

    while True:
        try:
            kwargs = dict(pipeline=pipeline, full_document="updateLookup")
            if resume_token:
                kwargs["resume_after"] = resume_token

            async with coll.watch(**kwargs) as stream:
                logging.info("Mongo change stream started")
                async for change in stream:
                    # Save token early to be resilient to restarts
                    token = change.get("_id")
                    if token:
                        resume_token = token
                        _save_resume_token(token)

                    doc = change.get("fullDocument") or {}
                    # IMPORTANT: adjust the key if your schema uses another field than "username"
                    student_name = doc.get("username")
                    if not student_name:
                        continue

                    # Debounce recompute for this student
                    now = asyncio.get_running_loop().time()
                    last = _debounce.get(student_name, 0.0)
                    if now - last < DEBOUNCE_SECONDS:
                        continue
                    _debounce[student_name] = now

                    application.create_task(recompute_student(application, student_name))

        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Change stream error — retry in 5s")
            await asyncio.sleep(5)

# ----------------- COMMANDS -----------------
async def on_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Reply with student's progress from cache; if cache is empty (e.g., after restart),
    compute on-the-fly and populate the cache before replying.
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    student_name = chat_id_to_student(chat_id)
    if not student_name:
        await context.bot.send_message(chat_id, "⛔️ You don't have access to this bot.")
        return

    # 1) Try cached preformatted message
    cached_msg = context.application.bot_data.get(PROGRESS_MSG_KEY.format(chat_id=chat_id))
    if cached_msg:
        await context.bot.send_message(chat_id, cached_msg, parse_mode="Markdown")
        return

    # 2) No cache → compute quickly and cache
    try:
        await recompute_student(context.application, student_name)
        cached_msg = context.application.bot_data.get(PROGRESS_MSG_KEY.format(chat_id=chat_id))
        if not cached_msg:
            cached_nums = context.application.bot_data.get(PROGRESS_KEY.format(student=student_name), {})
            cached_msg = format_missing_tasks_markdown(cached_nums)
        await context.bot.send_message(chat_id, cached_msg, parse_mode="Markdown")
    except Exception:
        logging.exception("on_progress failed for %s", student_name)
        await context.bot.send_message(chat_id, "⚠️ Failed to get progress. Please try again later.")

# ----------------- PTB APP INIT -----------------
async def _post_init(app):
    # Register commands (aliases)
    app.add_handler(CommandHandler(["todo", "progress", "status"], on_progress))
    # Prewarm cache for all known students (runs in background)
    for student in USERS.keys():
        app.create_task(recompute_student(app, student))
    # Start Mongo watcher in background
    app.create_task(watch_answers(app))

if __name__ == '__main__':
    app = MyBot.run_bot(TOKEN)
    app.post_init = _post_init
    app.run_polling()
