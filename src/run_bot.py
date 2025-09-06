# run_bot.py  — event-based вариант, без JobQueue и без потоков

import os
import asyncio
import logging
import configparser
from typing import Dict, Any
from motor.motor_asyncio import AsyncIOMotorClient
from telegram.ext import ContextTypes  # только для type hints

from get_creds import get_creds
from kosokolovsky_telegram_bot import MyBot
from main import doneOrNot  # синхронная/тяжёлая функция — уводим в to_thread

# ----------------- ЛОГИ -----------------
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

# creds_path = os.path.join(os.path.dirname(__file__), "../..", "creds.ini")
creds_path = "/Users/konstantinsokolovskiy/Desktop/web_scrapping/polyakov_23_24/files/tutor_telegram_bot/creds.ini"
config = configparser.ConfigParser()
config.read(creds_path)

dbname = config["MAIN"]["dbname"]
username = config["MAIN"]["username"]
password = config["MAIN"]["password"]
rds_endpoint = config["MAIN"]["rds_endpoint"]

# Mongo настройки — лучше вынести в переменные окружения или отдельный секшен в creds.ini
MONGO_URI = os.getenv("MONGO_URI", "mongodb://172.31.44.220:27017/?replicaSet=rs0")
DB_NAME = os.getenv("MONGO_DB", "log_db")
ANSWERS_COLL = os.getenv("MONGO_COLL", "logs")

# Резюм-токен для change stream (чтобы не терять события при рестарте)
RESUME_FILE = os.path.join(os.path.dirname(__file__), "cache", "mongo_resume_token.bson")

# Антидребезг — не чаще, чем раз в N секунд на ученика
DEBOUNCE_SECONDS = 3.0
_debounce: Dict[str, float] = {}

# Включать ли реальные отправки сообщений (иначе — только обновление bot_data)
SEND_MESSAGES = False


# ----------------- УТИЛИТЫ -----------------
def format_missing_tasks_markdown(missing_tasks: Dict[int, Any]) -> str:
    if not missing_tasks:
        return "✅ All is done. Enjoy the moment!"
    message = "*📌 ToDo:*\n\n"
    for task, nums in missing_tasks.items():
        nums_str = ", ".join(map(str, nums))
        message += f"• *Task {task}:* \n\t`{nums_str}`\n\n"
    return message


def _save_resume_token(token: Dict[str, Any]) -> None:
    if not token:
        return
    os.makedirs(os.path.dirname(RESUME_FILE), exist_ok=True)
    # сохраняем бинарно, чтобы не парсить вручную
    with open(RESUME_FILE, "wb") as f:
        # _id в change stream — это BSON документ; берём .binary из RawBSONDocument если надо
        from bson import BSON
        f.write(BSON.encode(token))


def _load_resume_token() -> Dict[str, Any] | None:
    try:
        from bson import BSON
        with open(RESUME_FILE, "rb") as f:
            return BSON(f.read()).decode()
    except FileNotFoundError:
        return None
    except Exception:
        logging.exception("Failed to load resume token")
        return None


# ----------------- НОТИФИКАЦИЯ УЧЕНИКА -----------------
async def notify_student(application, student_name: str) -> None:
    """Пересчитать прогресс и обновить bot_data/отправить сообщение конкретному ученику."""
    chat_id = USERS.get(student_name)
    if not chat_id:
        logging.debug(f"Unknown student '{student_name}', skip")
        return

    # doneOrNot — синхронный расчёт → уводим в thread pool
    nums = await asyncio.to_thread(doneOrNot, student_name=student_name)
    msg = format_missing_tasks_markdown(nums)

    # Антиспам: шлём/обновляем только если изменилось содержимое
    key = f"custom_message_{chat_id}"
    prev = application.bot_data.get(key)
    if prev == msg:
        logging.debug(f"No change for {student_name}, skip sending")
        return

    application.bot_data[key] = msg
    logging.info(f"Updated bot_data[{chat_id}] for {student_name}")

    if SEND_MESSAGES:
        try:
            await application.bot.send_message(chat_id, msg, parse_mode="Markdown")
        except Exception:
            logging.exception(f"Failed to send message to {chat_id}")


# ----------------- WATCHER MONGO -----------------
async def watch_answers(application) -> None:
    """
    Слушаем MongoDB Change Streams по коллекции ответов.
    На insert/update/replace триггерим notify_student для соответствующего ученика.
    """
    client = AsyncIOMotorClient(MONGO_URI)
    coll = client[DB_NAME][ANSWERS_COLL]

    pipeline = [
        {"$match": {"operationType": {"$in": ["insert", "update", "replace"]}}}
        # при желании фильтруй по предметам/классам тут
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
                    # сохраняем токен как можно раньше
                    token = change.get("_id")
                    if token:
                        resume_token = token
                        _save_resume_token(token)

                    doc = change.get("fullDocument") or {}
                    # подстрой ключ под твою схему документа
                    student_name = doc.get("username")
                    if not student_name:
                        continue

                    # Дебаунс: не чаще раза в N секунд на ученика
                    now = asyncio.get_running_loop().time()
                    last = _debounce.get(student_name, 0.0)
                    if now - last < DEBOUNCE_SECONDS:
                        continue
                    _debounce[student_name] = now

                    application.create_task(notify_student(application, student_name))

        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Change stream error — retry in 5s")
            await asyncio.sleep(5)


# ----------------- ИНИЦИАЛИЗАЦИЯ PTB -----------------
async def _post_init(app):
    # Запускаем watcher в том же event loop PTB
    app.create_task(watch_answers(app))


if __name__ == '__main__':
    app = MyBot.run_bot(TOKEN)
    app.post_init = _post_init
    app.run_polling()
