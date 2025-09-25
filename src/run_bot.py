import os
import asyncio
import logging
import configparser
import re
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


def _check_admin_identity():
    try:
        admin_id_cfg = MyBot.get_admin_id()  # из creds.ini [USERS][admin]
    except Exception as e:
        logging.error("get_admin_id failed: %s", e)
        admin_id_cfg = None

    admin_id_users = USERS.get("admin")      # из get_creds()

    logging.info("ADMIN CHECK: admin_id_cfg=%r, admin_id_users=%r", admin_id_cfg, admin_id_users)

    if admin_id_cfg is None or admin_id_users is None:
        logging.warning("ADMIN CHECK: admin missing in one of sources (cfg/users)")
        return

    try:
        admin_id_cfg_int = int(admin_id_cfg)
    except Exception:
        logging.warning("ADMIN CHECK: admin_id_cfg is not int: %r", admin_id_cfg)
        admin_id_cfg_int = admin_id_cfg

    try:
        admin_id_users_int = int(admin_id_users)
    except Exception:
        logging.warning("ADMIN CHECK: admin_id_users is not int: %r", admin_id_users)
        admin_id_users_int = admin_id_users

    if admin_id_cfg_int != admin_id_users_int:
        logging.error("ADMIN MISMATCH: creds.ini=%r vs USERS.get('admin')=%r", admin_id_cfg_int, admin_id_users_int)
    else:
        logging.info("ADMIN OK: %r", admin_id_cfg_int)


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

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/?authSource=admin&replicaSet=rs0")
DB_NAME = "log_db"
ANSWERS_COLL = "logs"

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

ADD_HW_RE = re.compile(r"^add_homework_to_(?P<user>[A-Za-z0-9_]+)$")

def _student_from_doc(doc: dict) -> Optional[str]:
    """
    Сначала пробуем распарсить целевого ученика из answer=add_homework_to_<user>
    (это наш «эхо»-формат после INSERT в MySQL).
    Если такого нет — берём прямые поля USER/username/user/student/name.
    """
    ans = doc.get("answer")
    if isinstance(ans, str):
        m = ADD_HW_RE.match(ans.strip())
        if m and m.group("user"):
            return m.group("user").strip().lower()

    for k in ("USER", "username", "user", "student", "name"):
        v = doc.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()

    return None

async def watch_answers(application) -> None:
    logging.info("Watcher starting. URI=%s DB=%s COLL=%s", MONGO_URI, DB_NAME, ANSWERS_COLL)
    client = AsyncIOMotorClient(MONGO_URI)
    coll = client[DB_NAME][ANSWERS_COLL]

    pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}}}]
    resume_token = _load_resume_token()

    while True:
        try:
            kwargs = dict(pipeline=pipeline, full_document="updateLookup")
            if resume_token:
                kwargs["resume_after"] = resume_token

            async with coll.watch(**kwargs) as stream:
                logging.info("Change stream started on %s.%s", DB_NAME, ANSWERS_COLL)
                async for change in stream:
                    logging.info("Change: op=%s _id=%s", change.get("operationType"), change.get("_id"))
                    token = change.get("_id")
                    if token:
                        resume_token = token
                        _save_resume_token(token)

                    doc = change.get("fullDocument") or {}
                    student_name = _student_from_doc(doc) or ""
                    logging.info("Doc username parsed: %r", student_name)
                    if not student_name:
                        continue


                    now = asyncio.get_running_loop().time()
                    if now - _debounce.get(student_name, 0.0) < DEBOUNCE_SECONDS:
                        continue
                    _debounce[student_name] = now

                    application.create_task(recompute_student(application, student_name))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception("Change stream error (%s). Retry in 5s", e)
            await asyncio.sleep(5)



# ----------------- COMMANDS -----------------
def _normalize_name(s: str) -> str:
    return s.strip().lower().replace("@", "")

def _name_to_chat_id(student_name: str) -> Optional[int]:
    return USERS.get(student_name)

async def debug_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    name = chat_id_to_student(chat_id) if chat_id else None
    try:
        admin_id_cfg = MyBot.get_admin_id()
    except Exception as e:
        admin_id_cfg = f"ERR: {e}"

    admin_id_users = USERS.get("admin")
    try:
        admin_equal = (int(chat_id) == int(admin_id_cfg))
    except Exception:
        admin_equal = False

    text = (
        f"chat_id={chat_id}\n"
        f"name={name}\n"
        f"admin_id(cfg)={admin_id_cfg}\n"
        f"admin_id(USERS)={admin_id_users}\n"
        f"is_admin={admin_equal}"
    )
    await context.bot.send_message(chat_id, text)


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /todo, /progress, /status, /check
    - non-admin: always returns their own missing HW (args ignored)
    - admin:
        * with arg: returns that student's missing HW
        * without arg: returns their own
    """
    if not update.effective_chat:
        return

    requester_chat_id = update.effective_chat.id
    try:
        admin_id_val = int(MyBot.get_admin_id())
    except Exception as e:
        logging.warning("get_admin_id failed: %s", e)
        admin_id_val = None

    is_admin = (admin_id_val is not None and requester_chat_id == admin_id_val)
    logging.info("ADMIN FLAG: requester=%r admin=%r is_admin=%r", requester_chat_id, admin_id_val, is_admin)


    target_chat_id: Optional[int] = None
    target_student_name: Optional[str] = None

    if is_admin and context.args:
        # admin requested a specific student
        arg_name = _normalize_name(context.args[0])
        cid = _name_to_chat_id(arg_name)
        if cid is None:
            await context.bot.send_message(
                requester_chat_id,
                "User not found"
            )
            return
        target_chat_id = cid
        target_student_name = arg_name
    else:
        # non-admin OR admin without args -> self
        target_chat_id = requester_chat_id
        target_student_name = chat_id_to_student(requester_chat_id)
        if not target_student_name:
            await context.bot.send_message(
                requester_chat_id,
                "⛔️ You don't have access to this bot."
            )
            return

    # Try cached, otherwise recompute and cache
    cache_key = PROGRESS_MSG_KEY.format(chat_id=target_chat_id)
    cached_msg = context.application.bot_data.get(cache_key)
    if not cached_msg:
        try:
            await recompute_student(context.application, target_student_name)
            cached_msg = context.application.bot_data.get(cache_key)
            if not cached_msg:
                # Fallback: build from raw dict if present
                raw = context.application.bot_data.get(
                    PROGRESS_KEY.format(student=target_student_name), {}
                )
                cached_msg = format_missing_tasks_markdown(raw)
        except Exception:
            logging.exception("on_status failed for %s", target_student_name)
            await context.bot.send_message(
                requester_chat_id,
                "⚠️ Failed to get progress. Please try again later."
            )
            return

    # Reply to the requester (admin or regular user)
    await context.bot.send_message(
        requester_chat_id,
        cached_msg,
        parse_mode="Markdown"
    )

# ----------------- PTB APP INIT -----------------
async def _post_init(app):
    app.add_handler(CommandHandler(["todo", "progress", "status", "check"], on_status))
    app.add_handler(CommandHandler(["debug_admin"], debug_admin))

    for student in USERS.keys():
        app.create_task(recompute_student(app, student))

    app.create_task(watch_answers(app))

if __name__ == '__main__':
    app = MyBot.run_bot(TOKEN)
    app.post_init = _post_init
    app.run_polling()
