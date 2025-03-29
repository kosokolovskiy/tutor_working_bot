from get_creds import get_creds
from kosokolovsky_telegram_bot import MyBot
from db_info.difference import DBAnalyzer
from connectors.mysql import SQLConnector
from main import doneOrNot
import asyncio
import threading
import configparser


# import nest_asyncio
# nest_asyncio.apply()


USERS, TOKEN, API_URL = get_creds()

config = configparser.ConfigParser()
config.read("creds.ini")


dbname = config["MAIN"]["dbname"]
username = config["MAIN"]["username"]
password = config["MAIN"]["password"]
rds_endpoint = config["MAIN"]["rds_endpoint"]

app = MyBot.run_bot(TOKEN)


def format_missing_tasks_markdown(missing_tasks):
    if not missing_tasks:
        return "✅ All is done. Enjoy the moment!"

    message = "*📌 ToDo:*\n\n"
    for task, nums in missing_tasks.items():
        nums_str = ", ".join(map(str, nums))
        message += f"• *Task {task}:* \n\t`{nums_str}`\n\n"

    return message


def get_student_name_from_chat(chat_id):
    return MyBot.students.get(chat_id, None)


obj = DBAnalyzer()


async def periodic_update_loop(app, interval, USERS):
    while True:
        print("Running periodic DB update...")
        try:
            for student_name in USERS.keys():
                nums = doneOrNot(student_name=student_name)
                formatted_message = format_missing_tasks_markdown(nums)
                chat_id = USERS[student_name]
                app.bot_data[f"custom_message_{chat_id}"] = formatted_message
        except Exception as e:
            print(f'ERROR during periodic update: {e}')
        await asyncio.sleep(interval)


def start_bot():
    asyncio.run(periodic_update_loop(app, interval=60, USERS=USERS))

if __name__ == '__main__':
    threading.Thread(target=start_bot, daemon=True).start()

    app.run_polling()
