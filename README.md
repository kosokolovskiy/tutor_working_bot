# Tutor Working Bot

Telegram bot for managing student homework.  
The bot integrates with **MySQL** (structured data) and **MongoDB** (logs, history), automatically sends notifications, caches results, and supports CI/CD deployment on **EC2**.

---

## 🎯 Background

This project was originally created as an extension of the **Tutor Streamlit App**,  
which provides a web interface for managing and reviewing homework tasks.  

While the Streamlit app works well for dashboards and manual control,  
it was missing a **lightweight real-time interface** for students and tutors.  

The **Tutor Working Bot** was developed to fill this gap by:

- Allowing students to directly interact with homework assignments through **Telegram**.  
- Sending **real-time notifications** and reminders (instead of relying on web updates).  
- Providing tutors with an **admin interface** inside Telegram (via `/hw_echo` and other commands).  
- Improving convenience: no need to log into a browser, students can check tasks instantly.  
- Adding redundancy: even if the web app is unavailable, Telegram bot ensures continuous access.  

In short, the bot complements the Streamlit app by focusing on **communication, caching, and quick access**.

---

## 📂 Project Structure

```
tutor_working_bot/
│
├── src/                 # main source code
│   ├── run_bot.py       # bot entry point
│   ├── send_notification.py  # notification logic
│   ├── get_creds.py     # credentials loader
│   └── db_info/         # database logic
│       ├── difference.py
│       └── ...
│
├── creds.ini            # credentials file (generated at deploy time)
├── requirements.txt     # dependencies
├── pyproject.toml       # modern dependency management
├── deploy-dev.yml       # CI/CD pipeline
├── README.md            # documentation
└── ...
```

---

## ⚡ Features

- Add and view student homework.
- Automatic student notifications.
- Supported commands: `/todo`, `/progress`, `/status`, `/check1`, `/hw_echo`.
- Query caching (reduces DB load).
- Multi-student support (from `USERS` list).
- Homework history stored in **MySQL** and **MongoDB**.
- CI/CD deployment: GitHub Actions → EC2 → systemd auto-start.

---

## 🔑 Configuration (`creds.ini`)

> **Never commit real secrets. Use placeholders and environment-specific secure stores.**

```ini
[TOKEN]
TELEGRAM_BOT_TOKEN = <your_telegram_bot_token>

[USERS]                 ; username -> telegram_id
admin = <telegram_id_admin>
student_1 = <telegram_id_student_1>
student_2 = <telegram_id_student_2>
# ...

[ID_USERS]              ; telegram_id -> username (reverse map)
<telegram_id_admin> = admin
<telegram_id_student_1> = student_1
<telegram_id_student_2> = student_2
# ...

[MAIN]                  ; MySQL connection
username = <mysql_username>
password = <mysql_password>
rds_endpoint = <mysql_endpoint_host>
dbname = <mysql_db_name>

[MONGO]                 ; MongoDB connection (example with authSource & replica set)
uri = mongodb://<mongo_user>:<mongo_password>@<mongo_host>:27017/?authSource=admin&replicaSet=<rs_name>
```

---

## ▶️ How the bot works

1. **Initialization**
   - Loads configuration from `creds.ini` via `get_creds.py`.
   - Registers commands.
   - Opens connections to **MySQL** and **MongoDB**.

2. **Database layers**
   - **MySQL** — primary storage: homework items, progress, student mappings.
   - **MongoDB** — event log & history: notifications, errors, cache refresh events, diffs.

3. **Difference logic (`db_info/difference.py`)**
   - Compares the latest cached snapshot with the current source-of-truth.
   - Writes deltas and audit info to MongoDB for traceability.
   - Can signal consumers to rebuild/refresh affected cache segments.

4. **Caching mechanism**
   - Admin triggers `/hw_echo <user>` — **refreshes cache in the database** for the given user.
   - Students read from cache for fast responses and reduced DB load.
   - All cache refreshes are logged (MongoDB) with timestamps and results.

5. **Commands**
   - `/todo` → List current homework.
   - `/progress` → Show progress.
   - `/status` → Student status.
   - `/check1` → Send test notification.
   - `/hw_echo <user>` → Refresh DB cache for given user (**admin-only**).

---

## 🚀 Installation

### 1) Clone the repository
```bash
git clone <your_repo_url>
cd tutor_working_bot
```

### 2) Install dependencies
```bash
pip install -r requirements.txt
# or: uv pip install -r requirements.txt
# or: poetry install
```

### 3) Add credentials
Place your `creds.ini` in the project root (never commit real secrets).

---

## ▶️ Running

### Locally
```bash
python src/run_bot.py
```

### On server (EC2)
Create a systemd unit (example):
```ini
[Unit]
Description=Tutor Working Bot
After=network.target

[Service]
User=<linux_user>
WorkingDirectory=/home/<linux_user>/tutor_working_bot
ExecStart=/usr/bin/python3 src/run_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Enable & start:
```bash
sudo systemctl enable tutor_bot
sudo systemctl start tutor_bot
```

---

## 🔄 CI/CD

- **GitHub Actions** (example): deploy on push to `main`.
- On EC2, `creds.ini` is generated/provisioned securely (not stored in git).
- The systemd service is restarted after deploy.

---

## 💬 Usage examples

```
/todo             # list of homework
/progress         # homework progress
/status           # student status
/check1           # test notification
/hw_echo <user>   # refresh cache in database (admin only)
```

---

## 🛡️ Security notes

- Do **not** hardcode or commit tokens, passwords, or real hostnames.
- Use placeholders in docs and templates, and secrets managers / instance metadata / SSM for real values.
- Rotate tokens regularly and restrict bot permissions to the minimum.

---

## 🛠 Roadmap

- [ ] Streamlit dashboard with progress overview
- [ ] Improved cache (Redis)
- [ ] GitLab CI/CD support
- [ ] Group assignments support
