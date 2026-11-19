# YBOT

I didn't like any of the other Discord bots out there, so I made my own.

YBOT is my personal assistant that lives in my Discord server. It:

- Manages my schedule, to-dos, and classes
- Saves my links and notes so I can search them later
- Connects to my Notion
- Sends me reminders on Discord by pinging me
- Posts a daily digest and a weekly review

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env   # add your bot token, user ID, and server ID
python bot.py
```

Or with Docker: `docker compose up -d --build`
