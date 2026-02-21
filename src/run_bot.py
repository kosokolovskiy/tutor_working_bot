import os
import asyncio
import logging
import configparser
import re
from typing import Dict, Any, Optional
from datetime import datetime

from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import ContextTypes, CommandHandler, InlineQueryHandler, ChosenInlineResultHandler

from motor.motor_asyncio import AsyncIOMotorClient
from bson import BSON

from get_creds import get_creds
from kosokolovsky_telegram_bot import MyBot
from main import doneOrNot, doneOrNotWithDates

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
        admin_id_cfg = MyBot.get_admin_id()
    except Exception as e:
        logging.error("get_admin_id failed: %s", e)
        admin_id_cfg = None

    admin_id_users = USERS.get("admin")

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


CREDS_PATH = os.getenv("CREDS_PATH")
config = configparser.ConfigParser()
config.read(CREDS_PATH)

print(CREDS_PATH)

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

async def _emit_homework_echo(target_username: str, by_username: str = "admin", event: str = "assigned_echo_from_bot") -> None:
    """Insert a small MongoDB ‘echo’ document (add_homework_to_<user>) to trigger the watcher to recompute that student’s cache."""
    try:
        client = AsyncIOMotorClient(MONGO_URI)
        coll = client[DB_NAME][ANSWERS_COLL]
        doc = {
            "task": 1,
            "num": 1,
            "answer": f"add_homework_to_{(target_username or '').strip().lower()}",
            "date": datetime.utcnow().strftime("%Y-%m-%d %H-%M-%S"),
            "username": by_username,
            "ts": datetime.utcnow(),
            "event": event
        }
        await coll.insert_one(doc)
        logging.info("BOT ECHO OK: %r", doc)
    except Exception:
        logging.exception("BOT ECHO FAILED for %s", target_username)


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

def format_missing_tasks_by_date_markdown(missing_tasks_by_date: Dict[str, Dict[int, Any]]) -> str:
    """Format result of doneOrNotWithDates into a Markdown message grouped by dates."""
    if not missing_tasks_by_date:
        return "✅ All is done. Enjoy the moment!"
    
    message = "*📌 ToDo по датам:*\n\n"
    
    # Сортируем даты для красивого отображения
    sorted_dates = sorted(missing_tasks_by_date.keys(), reverse=True)
    
    for date_str in sorted_dates:
        tasks_for_date = missing_tasks_by_date[date_str]
        message += f"📅 *{date_str}:*\n\n"
        
        for task, nums in sorted(tasks_for_date.items()):
            nums_str = ", ".join(map(str, nums))
            message += f"  • *Task {task}:* `{nums_str}`\n\n"
        
        message += "\n"
    
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
#   progress_by_date/<student_name>  -> raw dict from doneOrNotWithDates
#   progress_by_date_msg/<student_name>  -> formatted markdown message
PROGRESS_KEY = "progress/{student}"
PROGRESS_MSG_KEY = "progress_msg/{chat_id}"
PROGRESS_BY_DATE_KEY = "progress_by_date/{student}"
PROGRESS_BY_DATE_MSG_KEY = "progress_by_date_msg/{student}"
STUDENTS_LIST_CACHE_KEY = "students_list_cache"
STUDENTS_LIST_CACHE_TIMESTAMP_KEY = "students_list_cache_timestamp"

async def get_cached_student_data(application, student_name: str) -> Optional[str]:
    """
    Получает закэшированные данные студента (без группировки по датам).
    Если кэша нет - обновляет кэш.
    """
    chat_id = USERS.get(student_name)
    if chat_id:
        cache_key = PROGRESS_MSG_KEY.format(chat_id=chat_id)
        cached_msg = application.bot_data.get(cache_key)
        
        if cached_msg:
            logging.debug("Using cached data for student %s (without dates)", student_name)
            return cached_msg
    
    # Кэша нет - получаем и кэшируем
    try:
        nums = await asyncio.to_thread(doneOrNot, student_name=student_name)
        formatted_msg = format_missing_tasks_markdown(nums)
        
        # Кэшируем raw данные и форматированное сообщение
        application.bot_data[PROGRESS_KEY.format(student=student_name)] = nums
        if chat_id:
            application.bot_data[PROGRESS_MSG_KEY.format(chat_id=chat_id)] = formatted_msg
        
        logging.info("Cache updated for student %s (without dates)", student_name)
        return formatted_msg
    except Exception as e:
        logging.exception("Failed to get data for student %s: %s", student_name, e)
        return None

async def get_cached_student_data_by_date(application, student_name: str) -> Optional[str]:
    """
    Получает закэшированные данные студента с группировкой по датам.
    Если кэша нет - обновляет кэш.
    """
    cache_key = PROGRESS_BY_DATE_MSG_KEY.format(student=student_name)
    cached_msg = application.bot_data.get(cache_key)
    
    if cached_msg:
        logging.debug("Using cached data for student %s (by date)", student_name)
        return cached_msg
    
    # Кэша нет - получаем и кэшируем
    try:
        nums_by_date = await asyncio.to_thread(doneOrNotWithDates, student_name=student_name)
        formatted_msg = format_missing_tasks_by_date_markdown(nums_by_date)
        
        # Кэшируем оба варианта: raw данные и форматированное сообщение
        application.bot_data[PROGRESS_BY_DATE_KEY.format(student=student_name)] = nums_by_date
        application.bot_data[PROGRESS_BY_DATE_MSG_KEY.format(student=student_name)] = formatted_msg
        
        logging.info("Cache updated for student %s (by date)", student_name)
        return formatted_msg
    except Exception as e:
        logging.exception("Failed to get data for student %s: %s", student_name, e)
        return None

async def get_cached_students_list(application) -> Optional[list]:
    """
    Получает закэшированный список студентов для админа.
    Кэш обновляется раз в месяц (или если его нет).
    """
    # Проверяем кэш
    cached_list = application.bot_data.get(STUDENTS_LIST_CACHE_KEY)
    cache_timestamp = application.bot_data.get(STUDENTS_LIST_CACHE_TIMESTAMP_KEY, 0)
    
    # Кэш на месяц (30 дней = 2592000 секунд)
    MONTH_IN_SECONDS = 30 * 24 * 60 * 60
    current_time = asyncio.get_running_loop().time()
    
    if cached_list and (current_time - cache_timestamp) < MONTH_IN_SECONDS:
        logging.debug("Using cached students list (age: %.0f days)", (current_time - cache_timestamp) / 86400)
        return cached_list
    
    # Кэша нет или он устарел - создаем новый
    students_list = []
    for student_name in sorted(USERS.keys()):
        if student_name != "admin":
            students_list.append(student_name)
    
    application.bot_data[STUDENTS_LIST_CACHE_KEY] = students_list
    application.bot_data[STUDENTS_LIST_CACHE_TIMESTAMP_KEY] = current_time
    
    logging.info("Students list cache updated (%d students)", len(students_list))
    return students_list

async def recompute_student(application, student_name: str) -> None:
    """
    Recompute a student's progress and update the cache.
    No messages are sent here; only cache gets updated.
    Обновляет кэш для обоих форматов: обычный и с группировкой по датам.
    """
    try:
        # Обновляем обычный кэш (doneOrNot)
        nums = await asyncio.to_thread(doneOrNot, student_name=student_name)
        application.bot_data[PROGRESS_KEY.format(student=student_name)] = nums

        chat_id = USERS.get(student_name)
        if chat_id:
            msg = format_missing_tasks_markdown(nums)
            application.bot_data[PROGRESS_MSG_KEY.format(chat_id=chat_id)] = msg

        # Обновляем кэш с группировкой по датам (doneOrNotWithDates)
        try:
            nums_by_date = await asyncio.to_thread(doneOrNotWithDates, student_name=student_name)
            formatted_msg_by_date = format_missing_tasks_by_date_markdown(nums_by_date)
            
            application.bot_data[PROGRESS_BY_DATE_KEY.format(student=student_name)] = nums_by_date
            application.bot_data[PROGRESS_BY_DATE_MSG_KEY.format(student=student_name)] = formatted_msg_by_date
        except Exception as e:
            logging.warning("Failed to update by-date cache for %s: %s", student_name, e)

        logging.info("Cache updated for %s (both formats)", student_name)
    except Exception:
        logging.exception("Failed to recompute for %s", student_name)

# ----------------- MONGO WATCHER -----------------

ADD_HW_RE = re.compile(r"^add_homework_to_(?P<user>[A-Za-z0-9_]+)$")

def _student_from_doc(doc: dict) -> Optional[str]:
    """Extract target username from change doc: prefer answer=add_homework_to_<user>, else USER/username/user/student/name. Returns lowercase or None."""
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
    """Watch Mongo change stream, parse target student from each event, debounce, and schedule cache recompute; persists resume token."""
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
                    last = _debounce.get(student_name, 0.0)
                    if now - last < DEBOUNCE_SECONDS:
                        logging.info("Debounced event for %s (%.2fs < %.2fs)", student_name, now - last, DEBOUNCE_SECONDS)
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
    - non-admin: returns their own missing HW (args ignored) and sends a copy to admin
    - admin:
        * with arg: returns that student's missing HW (only to admin)
        * without arg: returns their own (only to admin)
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
        arg_name = _normalize_name(context.args[0])
        cid = _name_to_chat_id(arg_name)
        if cid is None:
            await context.bot.send_message(requester_chat_id, "User not found")
            return
        target_chat_id = cid
        target_student_name = arg_name
    else:
        target_chat_id = requester_chat_id
        target_student_name = chat_id_to_student(requester_chat_id)
        if not target_student_name:
            if is_admin:
                await context.bot.send_message(
                    requester_chat_id,
                    "ℹ️ You are admin. Provide a student name as an argument, e.g. `/status Ivan`.",
                    parse_mode="Markdown",
                )
            else:
                await context.bot.send_message(
                    requester_chat_id,
                    "⛔️ You don't have access to this bot."
                )
            return

    # Always recompute to get fresh data (cache might be stale if MySQL was updated directly)
    try:
        await recompute_student(context.application, target_student_name)
        cache_key = PROGRESS_MSG_KEY.format(chat_id=target_chat_id)
        cached_msg = context.application.bot_data.get(cache_key)
        if not cached_msg:
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

    if is_admin:
        await context.bot.send_message(
            requester_chat_id,
            cached_msg,
            parse_mode="Markdown"
        )
    else:
        recipients = {requester_chat_id}
        if admin_id_val:
            recipients.add(admin_id_val)

        for chat_id in recipients:
            if chat_id == admin_id_val and chat_id != requester_chat_id:
                admin_copy = f"👤 *Student:* `{target_student_name}`\n\n{cached_msg}"
                await context.bot.send_message(chat_id, admin_copy, parse_mode="Markdown")
            else:
                await context.bot.send_message(chat_id, cached_msg, parse_mode="Markdown")


async def on_status_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /todo_date, /progress_date, /status_date, /check_date
    - non-admin: returns their own missing HW grouped by dates (args ignored) and sends a copy to admin
    - admin:
        * with arg: returns that student's missing HW grouped by dates (only to admin)
        * without arg: returns their own (only to admin)
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
        arg_name = _normalize_name(context.args[0])
        cid = _name_to_chat_id(arg_name)
        if cid is None:
            await context.bot.send_message(requester_chat_id, "User not found")
            return
        target_chat_id = cid
        target_student_name = arg_name
    else:
        target_chat_id = requester_chat_id
        target_student_name = chat_id_to_student(requester_chat_id)
        if not target_student_name:
            if is_admin:
                await context.bot.send_message(
                    requester_chat_id,
                    "ℹ️ You are admin. Provide a student name as an argument, e.g. `/status_date Ivan`.",
                    parse_mode="Markdown",
                )
            else:
                await context.bot.send_message(
                    requester_chat_id,
                    "⛔️ You don't have access to this bot."
                )
            return

    # Always recompute to get fresh data
    try:
        nums_by_date = await asyncio.to_thread(doneOrNotWithDates, student_name=target_student_name)
        formatted_msg = format_missing_tasks_by_date_markdown(nums_by_date)
    except Exception:
        logging.exception("on_status_date failed for %s", target_student_name)
        await context.bot.send_message(
            requester_chat_id,
            "⚠️ Failed to get progress. Please try again later."
        )
        return

    if is_admin:
        await context.bot.send_message(
            requester_chat_id,
            formatted_msg,
            parse_mode="Markdown"
        )
    else:
        recipients = {requester_chat_id}
        if admin_id_val:
            recipients.add(admin_id_val)

        for chat_id in recipients:
            if chat_id == admin_id_val and chat_id != requester_chat_id:
                admin_copy = f"👤 *Student:* `{target_student_name}`\n\n{formatted_msg}"
                await context.bot.send_message(chat_id, admin_copy, parse_mode="Markdown")
            else:
                await context.bot.send_message(chat_id, formatted_msg, parse_mode="Markdown")


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает inline query для todo и todo_date."""
    if not update.inline_query:
        logging.warning("on_inline_query: update.inline_query is None")
        return
    
    query = update.inline_query.query.strip() if update.inline_query.query else ""
    
    # Получаем ID пользователя, который делает запрос
    user_id = update.inline_query.from_user.id
    
    # ВАЖНО: Проверка безопасности ДО обработки запроса!
    # Проверяем, является ли пользователь админом
    try:
        admin_id_val = int(MyBot.get_admin_id())
    except Exception:
        admin_id_val = None
    
    is_admin = (admin_id_val is not None and user_id == admin_id_val)
    
    # Дополнительная проверка безопасности: пользователь должен быть в словаре USERS
    # Более строгая проверка с преобразованием всех значений к int
    user_is_in_system = False
    try:
        for name, cid in USERS.items():
            try:
                if int(cid) == user_id:
                    user_is_in_system = True
                    break
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logging.error("Error checking user_is_in_system: %s", e)
        user_is_in_system = False
    
    # Логируем для отладки
    logging.info("Inline query received from user %s: %r (is_admin=%s, in_system=%s)", 
                 user_id, query, is_admin, user_is_in_system)
    
    # Если пользователь не в системе - сразу блокируем доступ для ЛЮБОГО запроса
    if not user_is_in_system:
        logging.warning("Unauthorized inline query attempt from user_id=%s (not in USERS dict), query=%r", user_id, query)
        results = [
            InlineQueryResultArticle(
                id="unauthorized",
                title="⛔️ Доступ запрещен",
                description="Ваш ID не найден в системе",
                input_message_content=InputTextMessageContent(
                    "⛔️ У вас нет доступа к этому боту. Обратитесь к администратору.",
                    parse_mode="Markdown"
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=1)
        return
    
    # Определяем тип запроса: "todo" или "todo_date"
    is_todo_date = query.startswith("todo_date")
    is_todo = query.startswith("todo") and not is_todo_date
    
    if not (is_todo_date or is_todo):
        # Если запрос не начинается с todo или todo_date, возвращаем пустой результат
        logging.debug("Inline query doesn't start with 'todo' or 'todo_date': %r", query)
        await update.inline_query.answer([], cache_time=1)
        return
    
    # Извлекаем имя студента из запроса
    parts = query.split(maxsplit=1)
    if len(parts) > 1:
        # Имя студента указано - проверяем права админа
        if not is_admin:
            results = [
                InlineQueryResultArticle(
                    id="not_admin",
                    title="⛔️ Доступ запрещен",
                    description="Только админ может запрашивать данные других студентов",
                    input_message_content=InputTextMessageContent(
                        f"⛔️ Только админ может запрашивать данные других студентов. Используйте `{'todo' if is_todo else 'todo_date'}` без имени для просмотра своих заданий.",
                        parse_mode="Markdown"
                    )
                )
            ]
            await update.inline_query.answer(results, cache_time=1)
            return
        
        student_name = _normalize_name(parts[1])
        
        # Проверяем, существует ли студент
        if student_name not in USERS:
            results = [
                InlineQueryResultArticle(
                    id="not_found",
                    title=f"❌ Студент '{student_name}' не найден",
                    description="Проверьте правильность имени",
                    input_message_content=InputTextMessageContent(
                        f"❌ Студент `{student_name}` не найден",
                        parse_mode="Markdown"
                    )
                )
            ]
            await update.inline_query.answer(results, cache_time=1)
            return
    else:
        # Имя не указано
        # ВАЖНО: Список студентов показываем ТОЛЬКО админу!
        # Дополнительная проверка безопасности перед показом списка
        # Проверяем еще раз, что пользователь точно админ из словаря USERS
        admin_id_from_users = None
        try:
            admin_id_from_users = int(USERS.get("admin", 0))
        except (ValueError, TypeError):
            admin_id_from_users = None
        
        # Строгая проверка: пользователь должен быть админом И быть в системе
        is_admin_strict = (admin_id_from_users is not None and 
                          user_id == admin_id_from_users and 
                          user_is_in_system)
        
        logging.info("Security check for student list: user_id=%s, admin_id_from_users=%s, is_admin=%s, user_is_in_system=%s, is_admin_strict=%s",
                    user_id, admin_id_from_users, is_admin, user_is_in_system, is_admin_strict)
        
        if is_admin_strict:
            query_type = "todo_date" if is_todo_date else "todo"
            logging.info("Admin verified (user_id=%s, admin_id=%s) requested %s without student name, showing all students", 
                        user_id, admin_id_from_users, query_type)
            
            # Получаем список студентов из кэша (кэш на месяц)
            students_list = await get_cached_students_list(context.application)
            
            if not students_list:
                results = [
                    InlineQueryResultArticle(
                        id="no_students",
                        title="❌ Нет доступных студентов",
                        description="В системе нет зарегистрированных студентов",
                        input_message_content=InputTextMessageContent(
                            "❌ В системе нет зарегистрированных студентов",
                            parse_mode="Markdown"
                        )
                    )
                ]
                await update.inline_query.answer(results, cache_time=5)
                return
            
            results = []
            
            # Используем кэш для данных студентов
            for student_name_in_list in students_list:
                try:
                    # Выбираем правильную функцию в зависимости от типа запроса
                    if is_todo_date:
                        formatted_msg = await get_cached_student_data_by_date(context.application, student_name_in_list)
                        result_id_prefix = "todo_date"
                    else:
                        formatted_msg = await get_cached_student_data(context.application, student_name_in_list)
                        result_id_prefix = "todo"
                    
                    if formatted_msg:
                        results.append(
                            InlineQueryResultArticle(
                                id=f"{result_id_prefix}_{student_name_in_list}",
                                title=f"📌 ToDo: {student_name_in_list}",
                                description=f"Показать невыполненные задания для {student_name_in_list}",
                                input_message_content=InputTextMessageContent(
                                    formatted_msg,
                                    parse_mode="Markdown"
                                )
                            )
                        )
                    else:
                        # Если данные не получены, добавляем результат с ошибкой
                        results.append(
                            InlineQueryResultArticle(
                                id=f"{result_id_prefix}_{student_name_in_list}_error",
                                title=f"❌ {student_name_in_list} (ошибка)",
                                description="Не удалось получить данные",
                                input_message_content=InputTextMessageContent(
                                    f"⚠️ Не удалось получить данные для `{student_name_in_list}`",
                                    parse_mode="Markdown"
                                )
                            )
                        )
                except Exception as e:
                    logging.warning("Failed to get cached data for student %s in inline query: %s", student_name_in_list, e)
                    result_id_prefix = "todo_date" if is_todo_date else "todo"
                    # Добавляем результат с ошибкой
                    results.append(
                        InlineQueryResultArticle(
                            id=f"{result_id_prefix}_{student_name_in_list}_error",
                            title=f"❌ {student_name_in_list} (ошибка)",
                            description="Не удалось получить данные",
                            input_message_content=InputTextMessageContent(
                                f"⚠️ Не удалось получить данные для `{student_name_in_list}`",
                                parse_mode="Markdown"
                            )
                        )
                    )
            
            if not results:
                results = [
                    InlineQueryResultArticle(
                        id="no_students",
                        title="❌ Нет доступных студентов",
                        description="В системе нет зарегистрированных студентов",
                        input_message_content=InputTextMessageContent(
                            "❌ В системе нет зарегистрированных студентов",
                            parse_mode="Markdown"
                        )
                    )
                ]
            
            # Кэш Telegram на максимальное время (чтобы снизить нагрузку)
            await update.inline_query.answer(results, cache_time=300)
            return
        
        # Если не админ - показываем только свои данные
        # Дополнительная проверка: если пользователь не админ, не показываем список всех студентов
        query_type = "todo_date" if is_todo_date else "todo"
        if not is_admin:
            logging.info("Non-admin user (user_id=%s) requested %s without name, showing only their own data", user_id, query_type)
        
        # Используем ID текущего пользователя для получения его собственных данных
        student_name = chat_id_to_student(user_id)
        if not student_name:
            # Если не нашли студента в словаре - это чужой пользователь
            logging.warning("Unauthorized inline query attempt from user_id=%s (not in USERS dict)", user_id)
            results = [
                InlineQueryResultArticle(
                    id="unauthorized",
                    title="⛔️ Доступ запрещен",
                    description="Ваш ID не найден в системе",
                    input_message_content=InputTextMessageContent(
                        "⛔️ У вас нет доступа к этому боту. Обратитесь к администратору.",
                        parse_mode="Markdown"
                    )
                )
            ]
            await update.inline_query.answer(results, cache_time=1)
            return
    
    # Получаем данные о заданиях из кэша
    try:
        logging.info("Processing inline query for student: %s (type=%s)", student_name, "todo_date" if is_todo_date else "todo")
        
        # Выбираем правильную функцию в зависимости от типа запроса
        if is_todo_date:
            formatted_msg = await get_cached_student_data_by_date(context.application, student_name)
            result_id_prefix = "todo_date"
            title_prefix = "📌 ToDo по датам:"
        else:
            formatted_msg = await get_cached_student_data(context.application, student_name)
            result_id_prefix = "todo"
            title_prefix = "📌 ToDo:"
        
        if not formatted_msg:
            raise Exception("Failed to get cached data")
        
        # Создаем результат для inline query
        results = [
            InlineQueryResultArticle(
                id=f"{result_id_prefix}_{student_name}",
                title=f"{title_prefix} {student_name}",
                description=f"Показать невыполненные задания для {student_name}",
                input_message_content=InputTextMessageContent(
                    formatted_msg,
                    parse_mode="Markdown"
                )
            )
        ]
        
        logging.info("Sending inline query results for student: %s", student_name)
        await update.inline_query.answer(results, cache_time=300)
        logging.info("Inline query results sent successfully")
    except Exception as e:
        logging.exception("Error in inline query for %s: %s", "todo_date" if is_todo_date else "todo", e)
        results = [
            InlineQueryResultArticle(
                id="error",
                title="❌ Ошибка при получении данных",
                description="Попробуйте позже",
                input_message_content=InputTextMessageContent(
                    "⚠️ Произошла ошибка при получении данных. Попробуйте позже."
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=1)


async def on_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает выбор результата inline query - отправляет сообщение админу, если это студент."""
    if not update.chosen_inline_result:
        logging.warning("on_chosen_inline_result: update.chosen_inline_result is None")
        return
    
    result_id = update.chosen_inline_result.result_id
    user_id = update.chosen_inline_result.from_user.id
    logging.info("Chosen inline result: result_id=%r, user_id=%s", result_id, user_id)
    
    # Проверяем, что это результат todo_date
    if not result_id.startswith("todo_date_"):
        logging.debug("Chosen inline result doesn't start with 'todo_date_': %r", result_id)
        return
    
    # Извлекаем имя студента из result_id
    student_name = result_id.replace("todo_date_", "")
    
    # Получаем ID пользователя, который выбрал результат
    user_id = update.chosen_inline_result.from_user.id
    
    # Проверяем, является ли пользователь админом
    try:
        admin_id_val = int(MyBot.get_admin_id())
    except Exception:
        admin_id_val = None
    
    is_admin = (admin_id_val is not None and user_id == admin_id_val)
    
    # Если это не админ (т.е. обычный студент), отправляем сообщение админу
    if not is_admin and admin_id_val:
        try:
            logging.info("Sending inline query result to admin for student %s (user_id=%s)", student_name, user_id)
            # Получаем данные для отправки админу
            nums_by_date = await asyncio.to_thread(doneOrNotWithDates, student_name=student_name)
            formatted_msg = format_missing_tasks_by_date_markdown(nums_by_date)
            
            # Формируем сообщение для админа с указанием студента
            admin_msg = f"👤 *Student:* `{student_name}`\n\n{formatted_msg}"
            
            # Отправляем сообщение админу
            await context.bot.send_message(admin_id_val, admin_msg, parse_mode="Markdown")
            logging.info("Successfully sent inline query result to admin for student %s", student_name)
        except Exception as e:
            logging.exception("Failed to send inline query result to admin for %s: %s", student_name, e)
    else:
        logging.info("Skipping admin notification: is_admin=%s, admin_id_val=%s", is_admin, admin_id_val)


async def on_hw_echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: /hw_echo <username> — insert Mongo 'add_homework_to_<username>' echo to trigger watcher cache recompute."""
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    try:
        admin_id_val = int(MyBot.get_admin_id())
    except Exception:
        admin_id_val = None

    if admin_id_val is None or chat_id != admin_id_val:
        await context.bot.send_message(chat_id, "⛔️ Admin only.")
        return

    if not context.args:
        await context.bot.send_message(chat_id, "Usage: /hw_echo <username>")
        return

    target_student = context.args[0].strip().lower()
    if target_student not in USERS:
        await context.bot.send_message(chat_id, f"User not found: {target_student}")
        return

    await _emit_homework_echo(target_student, by_username="admin", event="assigned_echo_from_bot")
    await context.bot.send_message(chat_id, f"✅ Echo inserted for {target_student}")


async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: /send <username> <message> — send a message to a specific student."""
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    try:
        admin_id_val = int(MyBot.get_admin_id())
    except Exception:
        admin_id_val = None

    if admin_id_val is None or chat_id != admin_id_val:
        await context.bot.send_message(chat_id, "⛔️ Admin only.")
        return

    if not context.args or len(context.args) < 2:
        await context.bot.send_message(chat_id, "Usage: /send <username> <message>")
        return

    target_student = _normalize_name(context.args[0])
    if target_student not in USERS:
        await context.bot.send_message(chat_id, f"User not found: {target_student}")
        return

    # Join all arguments after username as the message text
    message_text = " ".join(context.args[1:])
    # Заменяем буквальные \n на реальные переносы строк
    message_text = message_text.replace("\\n", "\n")
    
    if not message_text.strip():
        await context.bot.send_message(chat_id, "Message cannot be empty.")
        return

    target_chat_id = USERS.get(target_student)
    if not target_chat_id:
        await context.bot.send_message(chat_id, f"Chat ID not found for user: {target_student}")
        return

    try:
        # Отправляем сообщение с поддержкой Markdown для красивого форматирования
        await context.bot.send_message(target_chat_id, message_text, parse_mode="Markdown")
        await context.bot.send_message(chat_id, f"✅ Message sent to {target_student}")
        logging.info("Admin %s sent message to %s: %s", chat_id, target_student, message_text)
    except Exception as e:
        error_msg = f"Failed to send message to {target_student}: {e}"
        await context.bot.send_message(chat_id, f"❌ {error_msg}")
        logging.exception("Failed to send message to %s", target_student)


# ----------------- PTB APP INIT -----------------
async def _post_init(app):
    app.add_handler(CommandHandler(["todo", "progress", "status", "check"], on_status))
    app.add_handler(CommandHandler(["todod", "progress_date", "status_date", "check_date"], on_status_date))
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(ChosenInlineResultHandler(on_chosen_inline_result))
    app.add_handler(CommandHandler(["debug_admin"], debug_admin))
    app.add_handler(CommandHandler(["hw_echo"], on_hw_echo))
    app.add_handler(CommandHandler(["send"], on_send))

    for student in USERS.keys():
        app.create_task(recompute_student(app, student))

    app.create_task(watch_answers(app))


if __name__ == '__main__':
    print(CREDS_PATH)
    logging.info("CREDS_PATH: %r", CREDS_PATH)
    app = MyBot.run_bot(TOKEN)
    app.post_init = _post_init
    app.run_polling()


