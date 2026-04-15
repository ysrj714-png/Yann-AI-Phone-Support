import asyncio
import json
import os
import re
import smtplib
import traceback
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from openai import OpenAI
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.twiml.messaging_response import MessagingResponse

app = FastAPI()

# ─── Config ───────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TWILIO_SID     = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER  = os.environ.get("TWILIO_PHONE_NUMBER", "+18449704843")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER      = os.environ.get("SMTP_USER", "")
SMTP_PASS      = os.environ.get("SMTP_PASS", "")
PORT           = int(os.environ.get("PORT", 8000))

VOICE = "alloy"

PHONE_PERSONA = """You are a friendly and knowledgeable AI assistant for Yann's AI Support.
You help anyone who calls with advice or support on any topic.
- Greet every caller: "Thank you for calling Yann's AI Support! How can I help you today?"
- Listen carefully and give clear, helpful advice on any topic
- Keep responses SHORT — under 2 sentences — this is a phone call
- Before ending, ask: "Would you like me to send you a summary? I can text it to you or email it."
- If they want email: ask for their email address
- End with: "Thanks for calling Yann's AI Support. Have a great day!"
- If asked if you're an AI: "I'm a virtual assistant for Yann's AI Support" """

SMS_PERSONA = """You are a friendly AI assistant for Yann's AI Support.
Help anyone who texts with advice or support on any topic.
Keep replies SHORT — under 160 characters where possible.
Be warm, helpful and conversational. Sign off responses with - Yann's AI Support"""

# SMS session memory
sms_sessions: dict = {}
sms_last_active: dict = {}


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    key_status = f"set ({OPENAI_API_KEY[:8]}...)" if OPENAI_API_KEY else "MISSING ❌"
    return {
        "status": "ok",
        "service": "Yann's AI Support",
        "openai_key": key_status,
        "twilio_sid": "set" if TWILIO_SID else "MISSING ❌",
    }


# ══════════════════════════════════════════════════════════════════════════════
# PHONE CALL
# ══════════════════════════════════════════════════════════════════════════════

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


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    print("🔌 Twilio WebSocket connected")

    # Verify API key before doing anything
    if not OPENAI_API_KEY:
        print("❌ FATAL: OPENAI_API_KEY environment variable is not set!")
        await websocket.close()
        return

    print(f"🔑 OpenAI key: {OPENAI_API_KEY[:12]}...")

    stream_sid    = None
    caller_number = None
    call_start    = datetime.now()
    transcript    = []

    try:
        print("🔗 Connecting to OpenAI Realtime API...")
        async with websockets.connect(
            "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17",
            extra_headers=[
                ("Authorization", f"Bearer {OPENAI_API_KEY}"),
                ("OpenAI-Beta", "realtime=v1"),
            ],
            ping_interval=20,
            ping_timeout=10,
        ) as openai_ws:
            print("✅ OpenAI Realtime connected!")
            await initialize_voice_session(openai_ws)

            async def receive_from_twilio():
                nonlocal stream_sid, caller_number
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        if data["event"] == "start":
                            stream_sid    = data["start"]["streamSid"]
                            caller_number = data["start"].get("customParameters", {}).get("callerNumber", "Unknown")
                            print(f"▶️  Stream started: {stream_sid} | Caller: {caller_number}")
                        elif data["event"] == "media" and openai_ws.open:
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }))
                        elif data["event"] == "stop":
                            duration = (datetime.now() - call_start).seconds
                            print(f"⏹️  Call ended — {duration}s")
                            await deliver_transcript(caller_number, transcript, duration)
                except Exception as e:
                    print(f"❌ Twilio receive error: {e}")

            async def send_to_twilio():
                try:
                    async for msg in openai_ws:
                        response = json.loads(msg)
                        etype = response.get("type", "")

                        if etype == "response.audio.delta" and "delta" in response:
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": response["delta"]},
                            })

                        if etype == "response.content.done":
                            for part in response.get("content", []):
                                if part.get("type") == "text" and part.get("text"):
                                    transcript.append({"role": "Assistant", "text": part["text"]})
                                    print(f"🤖 {part['text'][:60]}...")

                        if etype == "error":
                            print(f"❌ OpenAI error event: {response}")

                except Exception as e:
                    print(f"❌ Send to Twilio error: {e}")

            await asyncio.gather(receive_from_twilio(), send_to_twilio())

    except Exception as e:
        print(f"❌ WebSocket handler error: {e}")
        traceback.print_exc()
        try:
            await websocket.close()
        except Exception:
            pass


async def initialize_voice_session(openai_ws):
    session = {
        "type": "session.update",
        "session": {
            "turn_detection":      {"type": "server_vad"},
            "input_audio_format":  "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice":               VOICE,
            "instructions":        PHONE_PERSONA,
            "modalities":          ["text", "audio"],
            "temperature":         0.75,
        },
    }
    await openai_ws.send(json.dumps(session))
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Greet the caller now."}],
        },
    }))
    await openai_ws.send(json.dumps({"type": "response.create"}))
    print("✅ OpenAI session initialised — greeting sent")


# ══════════════════════════════════════════════════════════════════════════════
# SMS
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-sms", methods=["GET", "POST"])
async def incoming_sms(request: Request):
    form        = await request.form()
    from_number = form.get("From", "")
    body        = form.get("Body", "").strip()
    print(f"💬 SMS from {from_number}: {body}")

    # Expire old sessions
    now = datetime.now()
    for k in [k for k, t in sms_last_active.items() if now - t > timedelta(minutes=30)]:
        sms_sessions.pop(k, None)
        sms_last_active.pop(k, None)

    if from_number not in sms_sessions:
        sms_sessions[from_number] = [{"role": "system", "content": SMS_PERSONA}]
    sms_last_active[from_number] = now
    sms_sessions[from_number].append({"role": "user", "content": body})

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        result = client.chat.completions.create(
            model="gpt-4o",
            messages=sms_sessions[from_number],
            max_tokens=300,
            temperature=0.75,
        )
        reply = result.choices[0].message.content.strip()
        sms_sessions[from_number].append({"role": "assistant", "content": reply})
    except Exception as e:
        print(f"❌ SMS OpenAI error: {e}")
        reply = "Sorry, having trouble right now. Please try again shortly!"

    twiml = MessagingResponse()
    twiml.message(reply)
    return PlainTextResponse(content=str(twiml), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# TRANSCRIPT DELIVERY
# ══════════════════════════════════════════════════════════════════════════════

async def deliver_transcript(caller_number, transcript, duration):
    if not transcript:
        return
    full_text   = " ".join(t["text"] for t in transcript)
    email_match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", full_text)
    if email_match:
        await send_email_transcript(email_match.group(0), transcript, duration, caller_number)
    else:
        await send_sms_transcript(caller_number, transcript, duration)


async def send_sms_transcript(to_number, transcript, duration):
    if not TWILIO_SID or not to_number or to_number == "Unknown":
        return
    mins, secs = divmod(duration, 60)
    lines = "\n".join(f"{t['role']}: {t['text']}" for t in transcript[-8:])
    body  = f"📞 Yann's AI Support\nDuration: {mins}m {secs}s\n━━━━━━━━━━\n{lines}\n━━━━━━━━━━\nThanks for calling!"
    if len(body) > 1600:
        body = body[:1597] + "..."
    try:
        TwilioClient(TWILIO_SID, TWILIO_TOKEN).messages.create(
            body=body, from_=TWILIO_NUMBER, to=to_number)
        print(f"✅ SMS transcript → {to_number}")
    except Exception as e:
        print(f"❌ SMS transcript error: {e}")


async def send_email_transcript(to_email, transcript, duration, caller_number):
    if not SMTP_USER or not SMTP_PASS:
        await send_sms_transcript(caller_number, transcript, duration)
        return
    mins, secs = divmod(duration, 60)
    lines = "\n".join(f"[{t['role']}]: {t['text']}" for t in transcript)
    body  = f"📞 Yann's AI Support — Call Transcript\n{'━'*40}\nDate: {datetime.now().strftime('%Y-%m-%d %H:%M')}\nDuration: {mins}m {secs}s\n{'━'*40}\n\n{lines}\n\n{'━'*40}\nThanks for calling Yann's AI Support!"
    msg           = MIMEText(body)
    msg["Subject"] = f"Your Yann's AI Support Summary — {datetime.now().strftime('%b %d, %H:%M')}"
    msg["From"]    = SMTP_USER
    msg["To"]      = to_email
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"✅ Email transcript → {to_email}")
    except Exception as e:
        print(f"❌ Email transcript error: {e}")


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
