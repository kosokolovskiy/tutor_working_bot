import os
from configparser import ConfigParser
from kosokolovsky_telegram_bot import MyBot 

def get_creds():
    CREDS_PATH = os.getenv("CREDS_PATH")

    config = ConfigParser()
    config.read(CREDS_PATH)

    USERS = {key: int(value) for key, value in config.items('USERS')}
    TOKENS = dict(config.items('TOKEN'))

    if TOKEN := TOKENS.get('telegram_bot_token', 0):
        API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        return USERS, TOKEN, API_URL
    else:
        raise ValueError('TELEGRAM_BOT_TOKEN is not found! Add to .env variables')