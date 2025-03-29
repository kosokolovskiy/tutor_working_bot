import configparser
from get_creds import get_creds
from kosokolovsky_telegram_bot import MyBot
import argparse
import requests
from db_info.difference import DBAnalyzer

from connectors.mysql import SQLConnector


config = configparser.ConfigParser()
config.read("config.ini") 
dbname = config["MAIN"]["dbname"]
username = config["MAIN"]["username"]
password = config["MAIN"]["password"]
rds_endpoint = config["MAIN"]["rds_endpoint"]


parser = argparse.ArgumentParser('TelegramBot Parser')
parser.add_argument('--username', type=str, required=True)
parser.add_argument('--homework', type=int, required=True)
args = parser.parse_args()

def format_missing_tasks_markdown(missing_tasks):
    if not missing_tasks:
        return "✅ All is done. Enjoy the moment!"

    message = "*📌 ToDo:*\n\n"
    for task, nums in missing_tasks.items():
        nums_str = ", ".join(map(str, nums))
        message += f"• *Task {task}:* \n\t`{nums_str}`\n\n"

    return message


USERS, TOKEN, API_URL = get_creds()

student_name = args.username 
homework = args.homework


bot = MyBot()

if homework:
    bot.send_notification(student_name, USERS[student_name], API_URL)
    bot.send_notification('admin', USERS['admin'], API_URL)
else:
    print('Start of Analysis...')
    obj = DBAnalyzer()
    filtered_df = obj._get_mongo_df(student_name)


    with SQLConnector(dbname, username, password, rds_endpoint) as sql_conn:
        nums = obj._compare_assigned_done(sql_conn, student_name, filtered_df)

    formatted_message = format_missing_tasks_markdown(nums)

    MyBot().send_notification_free(USERS[student_name], API_URL, formatted_message)