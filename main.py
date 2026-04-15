import asyncio
import json
import os
import re
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import websockets
from fastapi import FastAPI, WebSocket, Request, Form
from fastapi.responses import HTMLResponse
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect

app = FastAPI()

# ─── Config (set via environment variables in Render) ────────────────────────
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
TWILIO_SID      = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER   = os.environ.get("TWILIO_PHONE_NUMBER", "+18449704843")
SMTP_HOST       = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER       = os.environ.get("SMTP_USER", "")
SMTP_PASS       = os.environ.get("SMTP_PASS", "")
PORT            = int(os.environ.get("PORT", 8000))

VOICE = "alloy"  # alloy | echo | shimmer | nova | onyx | fable

SYSTEM_MESSAGE = """You are a friendly and knowledgeable AI assistant for Yann's AI Support.
You answer calls from anyone who needs help, advice, or support on any topic.

Your responsibilities:
- Greet every caller warmly: "Thank you for calling Yann's AI Support! How can I help you today?"
- Listen carefully and provide clear, helpful, honest advice on any topic they bring up
- Be conversational, warm, and concise — this is a phone call, keep responses brief
- If a question is complex, break it into simple steps
- Before ending the call, ALWAYS ask:
  "Would you like me to send you a summary of our conversation?
   I can text it to the number you're calling from, or email it to you — which would you prefer?"
- If they want email: ask for their email address and confirm it back to them
- If they want SMS: confirm you'll text their number
- If they don't want either: thank them and end politely
- Always end with: "Thanks for calling Yann's AI Support. Have a great day!"

Important rules:
- Never reveal you are built on OpenAI
- If asked if you're an AI, say: "I'm an AI assistant for Yann's AI Support"
- Be empathetic and non-judgmental — callers may need emotional support or practical advice
- Keep each response under 3 sentences where possible"""

LOG_EVENT_TYPES = [
    "error", "response.content.done", "rate_limits.updated",
    "response.done", "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped", "input_audio_buffer.speech_started",
    "session.created",
]


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return {"status": "ok", "service": "Yann's AI Support — Phone Assistant ✅"}


# ─── Twilio Incoming Call Webhook ─────────────────────────────────────────────
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    form = await request.form()
    caller_number = form.get("From", "Unknown")
    host = request.headers.get("host", request.url.hostname)

    print(f"📞 Incoming call from {caller_number}")

    response = VoiceResponse()
    response.pause(length=1)
    connect = Connect()
    stream = connect.stream(url=f"wss://{host}/media-stream")
    stream.parameter(name="callerNumber", value=caller_number)
    response.append(connect)

    return HTMLResponse(content=str(response), media_type="application/xml")


# ─── Twilio Media Stream WebSocket ────────────────────────────────────────────
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()

    stream_sid    = None
    caller_number = None
    call_start    = datetime.now()
    transcript    = []   # {"role": "assistant"|"caller", "text": str}

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        },
    ) as openai_ws:

        await initialize_session(openai_ws)

        # ── Receive audio from Twilio ──────────────────────────────────────
        async def receive_from_twilio():
            nonlocal stream_sid, caller_number
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)

                    if data["event"] == "start":
                        stream_sid    = data["start"]["streamSid"]
                        caller_number = data["start"].get("customParameters", {}).get("callerNumber", "Unknown")
                        print(f"▶️  Stream {stream_sid} | Caller: {caller_number}")

                    elif data["event"] == "media" and openai_ws.open:
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }))

                    elif data["event"] == "stop":
                        duration = (datetime.now() - call_start).seconds
                        print(f"⏹️  Call ended — {duration}s | {caller_number}")
                        await deliver_transcript(caller_number, transcript, duration)

            except Exception as e:
                print(f"❌ Twilio receive error: {e}")

        # ── Send audio + capture transcript from OpenAI ────────────────────
        async def send_to_twilio():
            nonlocal stream_sid
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    event_type = response.get("type", "")

                    # Stream audio back to caller
                    if event_type == "response.audio.delta" and "delta" in response:
                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": response["delta"]},
                        })

                    # Capture assistant transcript
                    if event_type == "response.content.done":
                        for part in response.get("content", []):
                            if part.get("type") == "text" and part.get("text"):
                                transcript.append({"role": "Assistant", "text": part["text"]})
                                print(f"🤖 Assistant: {part['text'][:80]}...")

                    # Capture caller transcript
                    if event_type == "conversation.item.created":
                        item = response.get("item", {})
                        if item.get("role") == "user":
                            for part in item.get("content", []):
                                if part.get("type") == "input_text" and part.get("text"):
                                    transcript.append({"role": "Caller", "text": part["text"]})

            except Exception as e:
                print(f"❌ OpenAI send error: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


# ─── Session Init ─────────────────────────────────────────────────────────────
async def initialize_session(openai_ws):
    await openai_ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "turn_detection":      {"type": "server_vad"},
            "input_audio_format":  "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice":               VOICE,
            "instructions":        SYSTEM_MESSAGE,
            "modalities":          ["text", "audio"],
            "temperature":         0.75,
        },
    }))
    # Trigger opening greeting
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Greet the caller now."}],
        },
    }))
    await openai_ws.send(json.dumps({"type": "response.create"}))
    print("✅ OpenAI session ready")


# ─── Transcript Delivery ──────────────────────────────────────────────────────
async def deliver_transcript(caller_number: str, transcript: list, duration: int):
    """
    Decide how to deliver the transcript:
    - If an email address was mentioned in the transcript → send email
    - Otherwise → send SMS to caller's number
    """
    if not transcript:
        print("⚠️  No transcript to deliver")
        return

    full_text = " ".join(t["text"] for t in transcript)

    # Try to find an email address in the transcript
    email_match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", full_text)

    if email_match:
        recipient_email = email_match.group(0)
        print(f"📧 Found email in transcript: {recipient_email} — sending email")
        await send_email_transcript(recipient_email, transcript, duration, caller_number)
    else:
        print(f"📱 No email found — sending SMS to {caller_number}")
        await send_sms_transcript(caller_number, transcript, duration)


async def send_sms_transcript(to_number: str, transcript: list, duration: int):
    if not TWILIO_SID or not TWILIO_TOKEN:
        print("⚠️  Twilio credentials not set — skipping SMS")
        return
    if not to_number or to_number == "Unknown":
        print("⚠️  Unknown caller number — skipping SMS")
        return

    mins, secs = divmod(duration, 60)
    lines = "\n".join(f"{t['role']}: {t['text']}" for t in transcript[-10:])  # last 10 exchanges
    body = (
        f"📞 Yann's AI Support — Call Summary\n"
        f"Duration: {mins}m {secs}s\n"
        f"━━━━━━━━━━━━━━\n"
        f"{lines}\n"
        f"━━━━━━━━━━━━━━\n"
        f"Thanks for calling Yann's AI Support!"
    )

    # Truncate to 1600 chars (SMS limit)
    if len(body) > 1600:
        body = body[:1597] + "..."

    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        msg = client.messages.create(body=body, from_=TWILIO_NUMBER, to=to_number)
        print(f"✅ SMS sent to {to_number} — SID: {msg.sid}")
    except Exception as e:
        print(f"❌ SMS failed: {e}")


async def send_email_transcript(to_email: str, transcript: list, duration: int, caller_number: str):
    if not SMTP_USER or not SMTP_PASS:
        print("⚠️  SMTP not configured — skipping email")
        # Fall back to SMS
        await send_sms_transcript(caller_number, transcript, duration)
        return

    mins, secs = divmod(duration, 60)
    lines = "\n".join(f"[{t['role']}]: {t['text']}" for t in transcript)

    body = f"""📞 Yann's AI Support — Call Transcript
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Date     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Duration : {mins}m {secs}s
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{lines}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Thanks for calling Yann's AI Support!
"""
    msg = MIMEText(body)
    msg["Subject"] = f"Your Yann's AI Support Call Summary — {datetime.now().strftime('%b %d, %H:%M')}"
    msg["From"]    = SMTP_USER
    msg["To"]      = to_email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"✅ Email transcript sent to {to_email}")
    except Exception as e:
        print(f"❌ Email failed: {e}")


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
