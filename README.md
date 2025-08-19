# Bite Balance Pro (FoodHub+)

A full-featured Flask app with:
- Email/OTP sign-up (Gmail SMTP)
- Profiles with avatar upload
- Blogs & Recipes (with images)
- Groups: **Public** and **Private (invite code)** + real-time chat (Socket.IO)
- AI Recipe suggestions via OpenAI (new `openai` SDK) with offline fallback

## Setup

```bash
python -m venv .venv
. .venv/Scripts/activate  # on Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env  # then edit it
```

Edit `.env`:
```
SECRET_KEY=some-random
OPENAI_API_KEY=sk-...
MAIL_USERNAME=your@gmail.com
MAIL_PASSWORD=your_app_password  # create in Google Account → Security → App passwords
```

Run:
```bash
python app.py
```
Open http://localhost:5000

If Socket.IO warns about eventlet, it will fall back to threading automatically.

## Notes
- AI page works without an API key (offline recipe), but set `OPENAI_API_KEY` for real AI.
- Private groups show an **Invite Code**. Share it. Others can join via **Join Private Group** form.
