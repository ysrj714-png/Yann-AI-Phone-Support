import asyncio
import os
import re
import requests
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import FastAPI, Request, BackgroundTasks
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
# PHONE — Step 1: Answer and ask text or email
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
async def choose_channel(request: Request, background_tasks: BackgroundTasks):
    form     = await request.form()
    speech   = form.get("SpeechResult", "").strip().lower()
    call_sid = request.query_params.get("call_sid", "unknown")
    caller   = request.query_params.get("caller", "Unknown")
    print(f"📲 [{call_sid[:8]}] Channel choice: '{speech}'")

    response = VoiceResponse()

    # ── TEXT chosen ───────────────────────────────────────────────────────────
    if any(w in speech for w in ["text", "sms", "message", "txt"]):
        response.say(
            "Ok, transferring to text now. You will receive a message shortly. Goodbye!",
            voice=VOICE,
        )
        response.hangup()

        # FIX: Use BackgroundTasks instead of await after hangup.
        # Twilio reads TwiML and hangs up BEFORE your async code resumes after
        # response.hangup(), which meant the SMS send was effectively orphaned.
        # BackgroundTasks runs AFTER the HTTP response is sent, which is correct.
        background_tasks.add_task(send_opening_sms, caller)
        return HTMLResponse(content=str(response), media_type="application/xml")

    # ── EMAIL chosen ──────────────────────────────────────────────────────────
    if any(w in speech for w in ["email", "e-mail", "mail"]):
        pending_email[call_sid] = {"caller": caller}

        # FIX: The original code had a redirect fallback inside the email branch
        # that pointed back to /choose-channel. When Gather timed out (caller
        # said nothing), it would redirect there — but with no SpeechResult, the
        # email branch was skipped and the "didn't understand" branch triggered,
        # effectively dropping the call loop. Changed the redirect to point back
        # to /collect-email so the prompt repeats on silence/timeout.
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
        # Timeout fallback: re-ask for email instead of going back to channel
        # selection (which was the original bug causing silent call drops).
        response.redirect(
            f"/collect-email?call_sid={call_sid}&caller={caller}&timeout=1",
            method="POST",
        )
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
async def collect_email(request: Request, background_tasks: BackgroundTasks):
    form     = await request.form()
    speech   = form.get("SpeechResult", "").strip()
    call_sid = request.query_params.get("call_sid", "unknown")
    caller   = request.query_params.get("caller", "Unknown")
    # Detect whether this is a timeout redirect (no speech input)
    is_timeout = request.query_params.get("timeout", "0") == "1"
    print(f"📧 [{call_sid[:8]}] Email speech: '{speech}'")

    response = VoiceResponse()

    # If this was a timeout redirect (Gather expired with no input), re-prompt.
    if is_timeout or not speech:
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={call_sid}&caller={caller}",
            method="POST",
            speech_timeout="auto",
            timeout=10,
        )
        gather.say(
            "I didn't hear anything. "
            "Please say your email address, for example: john at gmail dot com.",
            voice=VOICE,
        )
        response.append(gather)
        response.redirect(
            f"/collect-email?call_sid={call_sid}&caller={caller}&timeout=1",
            method="POST",
        )
        return HTMLResponse(content=str(response), media_type="application/xml")

    email = extract_email(speech)

    if email:
        print(f"✅ Email captured: {email}")
        response.say("Got it! Sending you an email now. Goodbye!", voice=VOICE)
        response.hangup()
        # FIX: Same BackgroundTasks pattern — ensures the email is sent AFTER
        # the TwiML response is returned, not blocked by Twilio's 15s timeout.
        background_tasks.add_task(send_opening_email, email)
        pending_email.pop(call_sid, None)
    else:
        # Couldn't parse the address — try again with a helpful example
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
        response.redirect(
            f"/collect-email?call_sid={call_sid}&caller={caller}&timeout=1",
            method="POST",
        )

    return HTMLResponse(content=str(response), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# OPENING MESSAGES sent after phone routing
# ══════════════════════════════════════════════════════════════════════════════

async def send_opening_sms(to_number: str):
    """Send the first SMS after caller chooses text.

    NOTE on Twilio toll-free verification:
    Until your toll-free number is verified, Twilio blocks outbound SMS to
    most US/CA numbers. This is a carrier-level restriction — the code is
    correct. Once verification is approved in the Twilio console
    (Phone Numbers → Manage → Regulatory Compliance), SMS will work without
    any code changes. In the meantime you can test with a Twilio trial number
    (non-toll-free) which has no such restriction.
    """
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
        print("⚠️  SMTP not configured — set SMTP_USER and SMTP_PASS env vars.")
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
        # FIX: Run the blocking SMTP call in a thread so it doesn't block the
        # FastAPI event loop. This was a subtle bug — smtplib is synchronous,
        # so calling it directly in an async function blocks the entire server
        # while it waits for the SMTP handshake (up to several seconds), which
        # can cause Twilio to time out waiting for a response.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _smtp_send, SMTP_USER, to_email, msg)
        print(f"✅ Opening email → {to_email}")
    except Exception as e:
        print(f"❌ Opening email error: {e}")


def _smtp_send(smtp_user: str, to_email: str, msg: MIMEMultipart):
    """Synchronous SMTP send — called via run_in_executor."""
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(smtp_user, SMTP_PASS)
        s.sendmail(smtp_user, to_email, msg.as_string())


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
    """Parse spoken email addresses like 'john at gmail dot com'.

    Also handles common speech-to-text quirks:
      - 'underscore' → '_'
      - 'dash' / 'hyphen' → '-'
      - 'period' → '.'
      - digits spoken as words (one, two … nine) → digit
    """
    t = text.lower().strip()

    # Spoken punctuation
    t = re.sub(r"\bunderscore\b", "_", t)
    t = re.sub(r"\b(dash|hyphen)\b", "-", t)
    t = re.sub(r"\bperiod\b", ".", t)

    # Spoken digits (optional but helpful for numeric local parts)
    digits = {"zero":"0","one":"1","two":"2","three":"3","four":"4",
              "five":"5","six":"6","seven":"7","eight":"8","nine":"9"}
    for word, digit in digits.items():
        t = re.sub(rf"\b{word}\b", digit, t)

    # Core spoken-email normalisation
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
