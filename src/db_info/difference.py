import pandas as pd
from connectors.mongodb import MongoConnector
from connectors.mysql import SQLConnector
import configparser
import os
import logging  # <<< добавили

import json
from datetime import date
from datetime import datetime, timedelta

from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/?authSource=admin&replicaSet=rs0")
DB_NAME = "log_db"
ANSWERS_COLL = "logs"
ADMIN_USERNAME = "admin"


creds_path = os.path.join(os.path.dirname(__file__), "../../creds.ini")
creds_path = "/home/ubuntu/tutor_bot/creds.ini"

config = configparser.ConfigParser()
config.read(creds_path)


dbname = config["MAIN"]["dbname"]
username = config["MAIN"]["username"]
password = config["MAIN"]["password"]
rds_endpoint = config["MAIN"]["rds_endpoint"]


def _echo_assigned_to_mongo(target_username: str, record_date: str) -> None:
    payload = {
        "task": 1,
        "num": 1,
        "answer": f"add_homework_to_{(target_username or '').strip().lower()}",
        "date": datetime.utcnow().strftime("%Y-%m-%d %H-%M-%S"),
        "username": ADMIN_USERNAME,
        "ts": datetime.utcnow(),
        "event": "assigned_echo"
    }
    try:
        client = MongoClient(MONGO_URI)
        client[DB_NAME][ANSWERS_COLL].insert_one(payload)
    except Exception as e:
        logging.warning("Mongo echo failed: %s", e)



class DBAnalyzer:
    def __init__(self):
        pass

    def _insert_homework_data(self, sql_connector, record_date, name, data_dict):
        columns = ['date', 'name'] + [f"`{i}`" for i in range(1, 28)]

        query = f"""
            INSERT INTO assigned ({', '.join(columns)})
            VALUES ({', '.join(['%s'] * len(columns))})
        """

        insert_values = [record_date, name] + [json.dumps(data_dict.get(i, [])) for i in range(1, 28)]

        sql_connector.cursor.execute(query, insert_values)
        sql_connector.connection.commit()


    def ingestHomework(self, test_d, record_date, name):
        with SQLConnector(dbname, username, password, rds_endpoint) as sql_conn:
            self._insert_homework_data(sql_conn, record_date, name, test_d)

        _echo_assigned_to_mongo(target_username=name, record_date=record_date)


    def findNotDone(self, student_name):
        print('Start of Search...')
        obj = DBAnalyzer()
        filtered_df = obj._get_mongo_df(student_name)

        with SQLConnector(dbname, username, password, rds_endpoint) as sql_conn:
            nums = obj._compare_assigned_done(sql_conn, student_name, filtered_df)
            print('Search is successful!')

        return nums

    def _get_mongo_df(self, username):
        stats = MongoConnector()
        df = stats.df
        return stats.filter_data(
            user=username, true_value=True, sort_column="D", ascending=False
        )

    

    def _compare_assigned_done(self, sql_connector, user_name, filtered_df, days_back=350):
        start_date = (datetime.now() - timedelta(days=days_back)).date().strftime('%Y-%m-%d')

        query_assigned = "SELECT * FROM assigned WHERE date > %s AND name = %s"
        sql_connector.cursor.execute(query_assigned, (start_date, user_name))
        assigned_results = sql_connector.cursor.fetchall()

        if not assigned_results:
            print("No assigned tasks found for this period and user.")
            return

        assigned_tasks = {}
        done_tasks = {}

        for assigned_row in assigned_results:
            for col_num in range(1, 28):
                assigned_raw = assigned_row[col_num + 1] or '[]'
                try:
                    nums = json.loads(assigned_raw)
                    assigned_tasks.setdefault(col_num + 1, set()).update(nums)
                except json.JSONDecodeError:
                    continue
        
        filtered_done = filtered_df[(filtered_df['D'] > start_date) & (filtered_df['USER'] == user_name)]
        
        for _, row in filtered_done.iterrows():
            task_num = int(row['task'])  
            num_value = int(row['num'])  
            done_tasks.setdefault(task_num, set()).add(num_value)

        missing_tasks = {}

        for col_num in assigned_tasks:
            missing = assigned_tasks[col_num] - done_tasks.get(col_num, set())
            if missing:
                missing_tasks[col_num] = sorted(missing)

        print(f"Missing tasks for {user_name} since {start_date}: {missing_tasks}")
        return missing_tasks