# Yann's AI Support — Phone Assistant

AI-powered phone answering system using **Twilio** + **OpenAI Realtime API**, deployed on **Render**.

Anyone who calls **(844) 970-4843** is answered by an AI that:
- Provides advice and support on any topic
- Sends a transcript via **SMS** to their phone number automatically
- Sends a transcript via **Email** if they provide their email during the call

---

## Deploy to Render

1. Upload all files to a GitHub repository
2. Connect the repo to [render.com](https://render.com) → New Web Service
3. Render auto-reads `render.yaml` — click Create
4. Optionally add `SMTP_USER` + `SMTP_PASS` for email transcript delivery
5. Copy your Render URL (e.g. `https://yanns-ai-support.onrender.com`)
6. Paste the URL here in the chat — the Twilio webhook will be configured automatically

## How It Works

```
Caller → Twilio → /incoming-call
                → /media-stream (WebSocket)
                → OpenAI Realtime API
                → Audio back to caller
                → Call ends → SMS or Email transcript sent to caller
```

## Customisation

- **Voice**: change `VOICE` in `main.py` (`alloy`, `echo`, `shimmer`, `nova`, `onyx`)
- **Personality**: edit `SYSTEM_MESSAGE` in `main.py`
