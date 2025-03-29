import os
from configparser import ConfigParser
from kosokolovsky_telegram_bot import MyBot 

def get_creds():
    creds_path = os.path.join(os.path.dirname(__file__), "../creds.ini")

    config = ConfigParser()
    config.read(creds_path)

    USERS = {key: int(value) for key, value in config.items('USERS')}
    TOKENS = dict(config.items('TOKEN'))

    if TOKEN := TOKENS.get('telegram_bot_token', 0):
        API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        return USERS, TOKEN, API_URL
    else:
        raise ValueError('TELEGRAM_BOT_TOKEN is not found! Add to .env variables')