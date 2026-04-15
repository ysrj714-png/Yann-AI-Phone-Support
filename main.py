import asyncio
import json
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from openai import OpenAI

import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.twiml.messaging_response import MessagingResponse

app = FastAPI()

# ─── Config ───────────────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
TWILIO_SID      = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER   = os.environ.get("TWILIO_PHONE_NUMBER", "+18449704843")
SMTP_HOST       = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER       = os.environ.get("SMTP_USER", "")
SMTP_PASS       = os.environ.get("SMTP_PASS", "")
PORT            = int(os.environ.get("PORT", 8000))

VOICE = "alloy"

PERSONA = """You are a friendly and knowledgeable AI assistant for Yann's AI Support.
You help anyone who reaches out — by phone, text, or email — with advice or support on any topic.
Be warm, concise, and genuinely helpful. Never reveal you are built on OpenAI.
If asked, say: "I'm an AI assistant for Yann's AI Support."
Keep responses conversational and to the point."""

PHONE_PERSONA = PERSONA + """
This is a phone call so keep each response under 3 sentences.
At the end of the call always ask if the caller wants a transcript via SMS or email."""

SMS_PERSONA = PERSONA + """
This is an SMS conversation. Keep replies SHORT — under 160 characters where possible.
Be helpful but snappy. End longer replies with a follow-up question to keep the conversation going."""

EMAIL_PERSONA = PERSONA + """
This is an email conversation. Write in a clear, professional but friendly tone.
Use short paragraphs. Sign off every reply as:

Best regards,
Yann's AI Support"""

# ─── SMS conversation memory (keyed by phone number, expires after 30 min) ───
sms_sessions: dict[str, list] = {}
sms_last_active: dict[str, datetime] = {}
SESSION_TTL_MINUTES = 30


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return {"status": "ok", "service": "Yann's AI Support ✅ — Phone | SMS | Email"}


# ══════════════════════════════════════════════════════════════════════════════
#  PHONE CALL HANDLING
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    form = await request.form()
    caller_number = form.get("From", "Unknown")
    host = request.headers.get("host", request.url.hostname)
    print(f"📞 Call from {caller_number}")

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
    stream_sid = None
    caller_number = None
    call_start = datetime.now()
    transcript = []

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17",
        additional_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        },
    ) as openai_ws:
        await initialize_voice_session(openai_ws)

        async def receive_from_twilio():
            nonlocal stream_sid, caller_number
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data["event"] == "start":
                        stream_sid = data["start"]["streamSid"]
                        caller_number = data["start"].get("customParameters", {}).get("callerNumber", "Unknown")
                    elif data["event"] == "media" and openai_ws.open:
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }))
                    elif data["event"] == "stop":
                        duration = (datetime.now() - call_start).seconds
                        await deliver_call_transcript(caller_number, transcript, duration)
            except Exception as e:
                print(f"❌ Twilio receive error: {e}")

        async def send_to_twilio():
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response.get("type") == "response.audio.delta" and "delta" in response:
                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": response["delta"]},
                        })
                    if response.get("type") == "response.content.done":
                        for part in response.get("content", []):
                            if part.get("type") == "text" and part.get("text"):
                                transcript.append({"role": "Assistant", "text": part["text"]})
                    if response.get("type") == "conversation.item.created":
                        item = response.get("item", {})
                        if item.get("role") == "user":
                            for part in item.get("content", []):
                                if part.get("type") == "input_text" and part.get("text"):
                                    transcript.append({"role": "Caller", "text": part["text"]})
            except Exception as e:
                print(f"❌ OpenAI error: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def initialize_voice_session(openai_ws):
    print("🔗 Connected to OpenAI Realtime API")
    await openai_ws.send(json.dumps({
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
    }))
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Greet the caller now."}],
        },
    }))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def deliver_call_transcript(caller_number, transcript, duration):
    if not transcript:
        return
    full_text = " ".join(t["text"] for t in transcript)
    email_match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", full_text)
    if email_match:
        await send_email_transcript(email_match.group(0), transcript, duration, caller_number)
    else:
        await send_sms_transcript(caller_number, transcript, duration)


# ══════════════════════════════════════════════════════════════════════════════
#  SMS HANDLING
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-sms", methods=["GET", "POST"])
async def incoming_sms(request: Request):
    form = await request.form()
    from_number = form.get("From", "")
    body        = form.get("Body", "").strip()
    print(f"💬 SMS from {from_number}: {body}")

    # Clean up expired sessions
    now = datetime.now()
    expired = [k for k, t in sms_last_active.items()
               if now - t > timedelta(minutes=SESSION_TTL_MINUTES)]
    for k in expired:
        sms_sessions.pop(k, None)
        sms_last_active.pop(k, None)

    # Get or create session for this number
    if from_number not in sms_sessions:
        sms_sessions[from_number] = [{"role": "system", "content": SMS_PERSONA}]
    sms_last_active[from_number] = now

    # Add user message
    sms_sessions[from_number].append({"role": "user", "content": body})

    # Get AI reply
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=sms_sessions[from_number],
            max_tokens=300,
            temperature=0.75,
        )
        reply = completion.choices[0].message.content.strip()
        sms_sessions[from_number].append({"role": "assistant", "content": reply})
    except Exception as e:
        print(f"❌ OpenAI SMS error: {e}")
        reply = "Sorry, I'm having trouble right now. Please try again in a moment!"

    # Reply via Twilio TwiML
    twiml = MessagingResponse()
    twiml.message(reply)
    return PlainTextResponse(content=str(twiml), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
#  EMAIL HANDLING  (called externally by Gmail trigger via this endpoint)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/incoming-email")
async def incoming_email(request: Request):
    data        = await request.json()
    sender      = data.get("from", "")
    subject     = data.get("subject", "")
    body        = data.get("body", "")
    reply_to    = data.get("reply_to", sender)
    print(f"📧 Email from {sender}: {subject}")

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system",  "content": EMAIL_PERSONA},
                {"role": "user",    "content": f"Subject: {subject}\n\n{body}"},
            ],
            temperature=0.75,
        )
        reply_body = completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ OpenAI email error: {e}")
        return {"status": "error", "detail": str(e)}

    if SMTP_USER and SMTP_PASS:
        msg = MIMEText(reply_body)
        msg["Subject"] = f"Re: {subject}"
        msg["From"]    = SMTP_USER
        msg["To"]      = reply_to
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_USER, reply_to, msg.as_string())
            print(f"✅ Email reply sent to {reply_to}")
        except Exception as e:
            print(f"❌ Email send error: {e}")

    return {"status": "ok", "reply": reply_body}


# ══════════════════════════════════════════════════════════════════════════════
#  TRANSCRIPT DELIVERY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def send_sms_transcript(to_number, transcript, duration):
    if not TWILIO_SID or not to_number or to_number == "Unknown":
        return
    mins, secs = divmod(duration, 60)
    lines = "\n".join(f"{t['role']}: {t['text']}" for t in transcript[-8:])
    body = f"📞 Yann's AI Support — Call Summary\nDuration: {mins}m {secs}s\n━━━━━━━━━━━━\n{lines}\n━━━━━━━━━━━━\nThanks for calling!"
    if len(body) > 1600:
        body = body[:1597] + "..."
    try:
        TwilioClient(TWILIO_SID, TWILIO_TOKEN).messages.create(
            body=body, from_=TWILIO_NUMBER, to=to_number)
        print(f"✅ SMS transcript → {to_number}")
    except Exception as e:
        print(f"❌ SMS error: {e}")


async def send_email_transcript(to_email, transcript, duration, caller_number):
    if not SMTP_USER or not SMTP_PASS:
        await send_sms_transcript(caller_number, transcript, duration)
        return
    mins, secs = divmod(duration, 60)
    lines = "\n".join(f"[{t['role']}]: {t['text']}" for t in transcript)
    body = f"📞 Yann's AI Support — Call Transcript\n{'━'*40}\nDate: {datetime.now().strftime('%Y-%m-%d %H:%M')}\nDuration: {mins}m {secs}s\n{'━'*40}\n\n{lines}\n\n{'━'*40}\nThanks for calling Yann's AI Support!"
    msg = MIMEText(body)
    msg["Subject"] = f"Your Yann's AI Support Call Summary — {datetime.now().strftime('%b %d, %H:%M')}"
    msg["From"]    = SMTP_USER
    msg["To"]      = to_email
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"✅ Email transcript → {to_email}")
    except Exception as e:
        print(f"❌ Email transcript error: {e}")


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
