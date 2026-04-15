import json
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from openai import OpenAI
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Gather
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

VOICE       = "Polly.Joanna"   # Natural AWS Polly voice via Twilio
AI_MODEL    = "gpt-4o"
SESSION_TTL = 30               # minutes before call session expires

SYSTEM_PROMPT = """You are a friendly and knowledgeable AI assistant for Yann's AI Support.
You help anyone who calls with advice or support on ANY topic — personal, business, technical, emotional, anything.

Rules:
- Keep EVERY response under 2 sentences — this is a phone call
- Be warm, direct, and genuinely helpful
- Never say you're built on OpenAI — if asked, say "I'm a virtual assistant for Yann's AI Support"
- When the conversation is ending, ask: "Before you go, would you like me to text or email you a summary of our chat?"
- If they want SMS: confirm you'll text their number automatically
- If they want email: ask for their email address clearly"""

SMS_PROMPT = """You are a friendly AI assistant for Yann's AI Support.
Help anyone who texts with advice or support on any topic.
Keep replies SHORT and conversational. Sign off as: — Yann's AI Support"""

# ─── In-memory call sessions ──────────────────────────────────────────────────
call_sessions: dict = {}      # call_sid → list of messages
call_last_active: dict = {}   # call_sid → datetime
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
# PHONE — Step 1: Incoming call → greet caller
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    form     = await request.form()
    call_sid = form.get("CallSid", "unknown")
    caller   = form.get("From", "Unknown")
    print(f"📞 Incoming call | SID: {call_sid} | From: {caller}")

    # Start fresh session for this call
    call_sessions[call_sid] = [{"role": "system", "content": SYSTEM_PROMPT}]
    call_last_active[call_sid] = datetime.now()

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/respond?call_sid={call_sid}&caller={caller}",
        method="POST",
        speech_timeout="auto",
        timeout=5,
        language="en-US",
    )
    gather.say(
        "Thank you for calling Yann's AI Support. How can I help you today?",
        voice=VOICE,
    )
    response.append(gather)
    # If caller doesn't speak, prompt again
    response.redirect("/incoming-call", method="POST")
    return HTMLResponse(content=str(response), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 2: Caller spoke → get AI reply → speak it → loop
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/respond", methods=["GET", "POST"])
async def respond(request: Request):
    form         = await request.form()
    speech_text  = form.get("SpeechResult", "").strip()
    call_sid     = request.query_params.get("call_sid", "unknown")
    caller       = request.query_params.get("caller", "Unknown")
    print(f"🗣️  Caller said: {speech_text}")

    response = VoiceResponse()

    if not speech_text:
        gather = Gather(
            input="speech",
            action=f"/respond?call_sid={call_sid}&caller={caller}",
            method="POST",
            speech_timeout="auto",
            timeout=5,
        )
        gather.say("I didn't catch that — could you say that again?", voice=VOICE)
        response.append(gather)
        return HTMLResponse(content=str(response), media_type="application/xml")

    # Get or create session
    if call_sid not in call_sessions:
        call_sessions[call_sid] = [{"role": "system", "content": SYSTEM_PROMPT}]
    call_sessions[call_sid].append({"role": "user", "content": speech_text})
    call_last_active[call_sid] = datetime.now()

    # Check if caller is giving their email in this turn
    email_match = re.search(
        r"[a-zA-Z0-9._%+\-]+\s*(?:at|@)\s*[a-zA-Z0-9.\-]+\s*(?:dot|\.)\s*[a-zA-Z]{2,}",
        speech_text, re.IGNORECASE
    )

    # Get AI response
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        result = client.chat.completions.create(
            model=AI_MODEL,
            messages=call_sessions[call_sid],
            max_tokens=150,
            temperature=0.75,
        )
        ai_reply = result.choices[0].message.content.strip()
        call_sessions[call_sid].append({"role": "assistant", "content": ai_reply})
        print(f"🤖 AI: {ai_reply}")
    except Exception as e:
        print(f"❌ OpenAI error: {e}")
        ai_reply = "I'm sorry, I'm having a brief issue. Please stay on the line."

    # Detect call ending keywords to send transcript
    ending_words = ["bye", "goodbye", "thank you", "thanks", "that's all", "hang up"]
    is_ending = any(w in speech_text.lower() for w in ending_words)

    if is_ending:
        # Send transcript before hanging up
        transcript = [
            m for m in call_sessions.get(call_sid, [])
            if m["role"] != "system"
        ]
        duration = int((datetime.now() - call_last_active.get(call_sid, datetime.now())).seconds)

        # Check for email in full conversation
        full_text   = " ".join(m["content"] for m in transcript)
        clean_email = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", full_text)
        if clean_email:
            await send_email_transcript(clean_email.group(0), transcript, duration, caller)
        else:
            await send_sms_transcript(caller, transcript, duration)

        # Clean up session
        call_sessions.pop(call_sid, None)
        call_last_active.pop(call_sid, None)

        response.say(ai_reply, voice=VOICE)
        response.say("Goodbye! Have a wonderful day.", voice=VOICE)
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")

    # Continue conversation
    gather = Gather(
        input="speech",
        action=f"/respond?call_sid={call_sid}&caller={caller}",
        method="POST",
        speech_timeout="auto",
        timeout=8,
    )
    gather.say(ai_reply, voice=VOICE)
    response.append(gather)

    # If no response after AI speaks, prompt gently
    response.redirect(f"/respond?call_sid={call_sid}&caller={caller}", method="POST")
    return HTMLResponse(content=str(response), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# SMS
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-sms", methods=["GET", "POST"])
async def incoming_sms(request: Request):
    form        = await request.form()
    from_number = form.get("From", "")
    body        = form.get("Body", "").strip()
    print(f"💬 SMS from {from_number}: {body}")

    now = datetime.now()
    for k in [k for k, t in sms_last_active.items() if now - t > timedelta(minutes=SESSION_TTL)]:
        sms_sessions.pop(k, None)
        sms_last_active.pop(k, None)

    if from_number not in sms_sessions:
        sms_sessions[from_number] = [{"role": "system", "content": SMS_PROMPT}]
    sms_last_active[from_number] = now
    sms_sessions[from_number].append({"role": "user", "content": body})

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        result = client.chat.completions.create(
            model=AI_MODEL,
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

async def send_sms_transcript(to_number, transcript, duration):
    if not TWILIO_SID or not to_number or to_number == "Unknown":
        return
    mins, secs = divmod(duration, 60)
    lines = "\n".join(f"{m['role'].title()}: {m['content']}" for m in transcript[-8:])
    body  = f"📞 Yann's AI Support\nDuration: {mins}m {secs}s\n{'━'*14}\n{lines}\n{'━'*14}\nThanks for calling!"
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
    lines = "\n".join(f"[{m['role'].title()}]: {m['content']}" for m in transcript)
    body  = f"📞 Yann's AI Support — Call Transcript\n{'━'*40}\nDate: {datetime.now().strftime('%Y-%m-%d %H:%M')}\nDuration: {mins}m {secs}s\n{'━'*40}\n\n{lines}\n\n{'━'*40}\nThanks for calling Yann's AI Support!"
    msg            = MIMEText(body)
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
        print(f"❌ Email error: {e}")


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
