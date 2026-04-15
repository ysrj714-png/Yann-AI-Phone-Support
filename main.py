import os
import re
import requests
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
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

VOICE = "Polly.Joanna"

SMS_SYSTEM = """You are a friendly and knowledgeable AI assistant for Yann's AI Support.
Help anyone who messages you with advice or support on ANY topic — personal, business, technical, emotional, anything.
Be warm, genuine, and helpful. Keep replies conversational.
Sign every reply with: — Yann's AI Support"""

EMAIL_SYSTEM = """You are a friendly and knowledgeable AI assistant for Yann's AI Support.
Help anyone who emails you with advice or support on ANY topic — personal, business, technical, emotional, anything.
Write in a clear, warm, professional tone. Use short paragraphs.
Sign every reply with:
Best regards,
Yann's AI Support"""

# ─── Session memory ───────────────────────────────────────────────────────────
sms_sessions:    dict = {}   # phone_number → [messages]
sms_last_active: dict = {}   # phone_number → datetime

# ─── Pending email collection ─────────────────────────────────────────────────
pending_email: dict = {}     # call_sid → {"caller": "+1...", "stage": "collecting"}


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return {
        "status":     "ok",
        "service":    "Yann's AI Support",
        "openai_key": f"set ({OPENAI_API_KEY[:8]}...)" if OPENAI_API_KEY else "MISSING ❌",
        "twilio":     "set" if TWILIO_SID else "MISSING ❌",
    }


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 1: Answer and immediately ask text or email
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    form     = await request.form()
    call_sid = form.get("CallSid", "unknown")
    caller   = form.get("From", "Unknown")
    print(f"📞 Call from {caller} | SID: {call_sid}")

    response = VoiceResponse()
    gather   = Gather(
        input="speech",
        action=f"/choose-channel?call_sid={call_sid}&caller={caller}",
        method="POST",
        speech_timeout="auto",
        timeout=6,
        language="en-US",
    )
    gather.say(
        "Thank you for calling Yann's AI Support. "
        "Would you like to continue with email or text?",
        voice=VOICE,
    )
    response.append(gather)
    # If caller doesn't respond, ask again
    response.redirect("/incoming-call", method="POST")
    return HTMLResponse(content=str(response), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 2: Caller chose email or text
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/choose-channel", methods=["GET", "POST"])
async def choose_channel(request: Request):
    form     = await request.form()
    speech   = form.get("SpeechResult", "").strip().lower()
    call_sid = request.query_params.get("call_sid", "unknown")
    caller   = request.query_params.get("caller", "Unknown")
    print(f"📲 [{call_sid[:8]}] Channel choice: '{speech}'")

    response = VoiceResponse()

    # ── TEXT chosen ────────────────────────────────────────────────────────────
    if any(w in speech for w in ["text", "sms", "message", "txt"]):
        response.say(
            "Ok, transferring to text now. You will receive a message shortly. Goodbye!",
            voice=VOICE,
        )
        response.hangup()
        # Fire-and-forget: send opening SMS
        await send_opening_sms(caller)
        return HTMLResponse(content=str(response), media_type="application/xml")

    # ── EMAIL chosen ───────────────────────────────────────────────────────────
    if any(w in speech for w in ["email", "e-mail", "mail"]):
        pending_email[call_sid] = {"caller": caller}
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={call_sid}&caller={caller}",
            method="POST",
            speech_timeout="auto",
            timeout=10,
            language="en-US",
        )
        gather.say("What is your email address?", voice=VOICE)
        response.append(gather)
        response.redirect(f"/choose-channel?call_sid={call_sid}&caller={caller}", method="POST")
        return HTMLResponse(content=str(response), media_type="application/xml")

    # ── Couldn't understand — ask again ───────────────────────────────────────
    gather = Gather(
        input="speech",
        action=f"/choose-channel?call_sid={call_sid}&caller={caller}",
        method="POST",
        speech_timeout="auto",
        timeout=6,
    )
    gather.say("Sorry, I didn't catch that. Please say email or text.", voice=VOICE)
    response.append(gather)
    return HTMLResponse(content=str(response), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 3: Collect email address
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/collect-email", methods=["GET", "POST"])
async def collect_email(request: Request):
    form     = await request.form()
    speech   = form.get("SpeechResult", "").strip()
    call_sid = request.query_params.get("call_sid", "unknown")
    caller   = request.query_params.get("caller", "Unknown")
    print(f"📧 [{call_sid[:8]}] Email speech: '{speech}'")

    response = VoiceResponse()
    email    = extract_email(speech)

    if email:
        print(f"✅ Email captured: {email}")
        response.say("Sending email now. Goodbye!", voice=VOICE)
        response.hangup()
        await send_opening_email(email)
        pending_email.pop(call_sid, None)
    else:
        # Couldn't parse — try again
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={call_sid}&caller={caller}",
            method="POST",
            speech_timeout="auto",
            timeout=10,
        )
        gather.say(
            "Sorry, I didn't catch that. "
            "Please say your email address slowly, for example: john at gmail dot com.",
            voice=VOICE,
        )
        response.append(gather)

    return HTMLResponse(content=str(response), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# OPENING MESSAGES sent after phone routing
# ══════════════════════════════════════════════════════════════════════════════

async def send_opening_sms(to_number: str):
    """Send the first SMS after caller chooses text."""
    if not TWILIO_SID or not to_number or to_number == "Unknown":
        return
    body = (
        "👋 Hey! This is Yann's AI Support.\n\n"
        "What do you need my friend? I'm here to help with anything — "
        "just type your question right here! 😊"
    )
    try:
        TwilioClient(TWILIO_SID, TWILIO_TOKEN).messages.create(
            body=body, from_=TWILIO_NUMBER, to=to_number
        )
        print(f"✅ Opening SMS → {to_number}")
    except Exception as e:
        print(f"❌ Opening SMS error: {e}")


async def send_opening_email(to_email: str):
    """Send the first email after caller gives their address."""
    if not SMTP_USER or not SMTP_PASS:
        print("⚠️  SMTP not set — skipping opening email")
        return

    subject = "Yann's AI Support — We're Ready to Help! 👋"
    body    = (
        "Hey there!\n\n"
        "This is Yann's AI Support — you just called us and chose email.\n\n"
        "What do you need my friend? 😊\n\n"
        "Just reply to this email with your question and I'll get back to you right away "
        "with helpful advice on anything you need.\n\n"
        "Best regards,\n"
        "Yann's AI Support"
    )

    msg            = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = to_email
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"✅ Opening email → {to_email}")
    except Exception as e:
        print(f"❌ Opening email error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SMS — AI conversation
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-sms", methods=["GET", "POST"])
async def incoming_sms(request: Request):
    form        = await request.form()
    from_number = form.get("From", "")
    body        = form.get("Body", "").strip()
    print(f"💬 SMS from {from_number}: {body}")

    # Clean up expired sessions (30 min idle)
    now = datetime.now()
    for k in [k for k, t in sms_last_active.items()
              if now - t > timedelta(minutes=30)]:
        sms_sessions.pop(k, None)
        sms_last_active.pop(k, None)

    # Start or continue session
    if from_number not in sms_sessions:
        sms_sessions[from_number] = [{"role": "system", "content": SMS_SYSTEM}]
    sms_last_active[from_number] = now
    sms_sessions[from_number].append({"role": "user", "content": body})

    reply = call_openai(sms_sessions[from_number], max_tokens=300)
    if reply:
        sms_sessions[from_number].append({"role": "assistant", "content": reply})
    else:
        reply = "Sorry, I'm having a brief issue. Please try again in a moment! — Yann's AI Support"

    twiml = MessagingResponse()
    twiml.message(reply)
    return PlainTextResponse(content=str(twiml), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def call_openai(messages: list, max_tokens: int = 300) -> str | None:
    """Call OpenAI directly via requests — no SDK, no proxy issues."""
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "gpt-4o",
                "messages":    messages,
                "max_tokens":  max_tokens,
                "temperature": 0.75,
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"❌ OpenAI error: {e}")
        return None


def extract_email(text: str) -> str | None:
    """Parse spoken email addresses like 'john at gmail dot com'."""
    t = text.lower().strip()
    t = re.sub(r"\s+at\s+",  "@", t)
    t = re.sub(r"\s+dot\s+", ".", t)
    t = re.sub(r"\s+",       "",  t)
    match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", t)
    return match.group(0) if match else None


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), reload=False)
