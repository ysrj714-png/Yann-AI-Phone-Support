import asyncio
import email as email_lib
import email.utils as email_utils
import imaplib
import os
import re
import requests
import smtplib
from datetime import datetime, timedelta
from email.header import decode_header as _decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, PlainTextResponse
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse

app = FastAPI()

# ─── Config ───────────────────────────────────────────────────────────────────
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
TWILIO_SID          = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN        = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER       = os.environ.get("TWILIO_PHONE_NUMBER", "+18449704843")
SMTP_HOST           = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT           = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER           = os.environ.get("SMTP_USER", "")
SMTP_PASS           = os.environ.get("SMTP_PASS", "")
IMAP_HOST           = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT           = int(os.environ.get("IMAP_PORT", 993))
EMAIL_POLL_INTERVAL = int(os.environ.get("EMAIL_POLL_INTERVAL", 30))

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
sms_sessions:      dict = {}  # phone → [messages]
sms_last_active:   dict = {}  # phone → datetime
email_sessions:    dict = {}  # email → [messages]
email_last_active: dict = {}  # email → datetime
email_processed:   set  = set()  # IMAP UIDs already handled this session
pending_email:     dict = {}  # call_sid → {"caller": "+1..."}


# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return {
        "status":  "ok",
        "service": "Yann's AI Support",
        "openai":  f"set ({OPENAI_API_KEY[:8]}...)" if OPENAI_API_KEY else "MISSING ❌",
        "twilio":  "set" if TWILIO_SID else "MISSING ❌",
        "smtp":    "set" if SMTP_USER else "MISSING ❌",
    }


# ─── Startup ──────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    if SMTP_USER and SMTP_PASS:
        asyncio.create_task(email_inbox_poller())
        print(f"📬 Email poller started (every {EMAIL_POLL_INTERVAL}s) → {SMTP_USER}")
    else:
        print("⚠️  SMTP not configured — email polling disabled")


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 1: answer and ask text or email
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    form     = await request.form()
    call_sid = form.get("CallSid", "unknown")
    caller   = form.get("From", "Unknown")
    print(f"📞 Call from {caller} | SID: {call_sid}")

    safe_sid    = quote(call_sid, safe="")
    safe_caller = quote(caller,   safe="")

    resp   = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/choose-channel?call_sid={safe_sid}&caller={safe_caller}",
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
    resp.append(gather)
    resp.redirect("/incoming-call", method="POST")
    return HTMLResponse(content=str(resp), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 2: caller chose email or text
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/choose-channel", methods=["GET", "POST"])
async def choose_channel(request: Request, background_tasks: BackgroundTasks):
    form     = await request.form()
    speech   = form.get("SpeechResult", "").strip().lower()
    call_sid = unquote(request.query_params.get("call_sid", "unknown"))
    caller   = unquote(request.query_params.get("caller",   "Unknown"))
    safe_sid    = quote(call_sid, safe="")
    safe_caller = quote(caller,   safe="")
    print(f"📲 [{call_sid[:8]}] Channel choice: '{speech}'")

    resp = VoiceResponse()

    # ── TEXT ──────────────────────────────────────────────────────────────────
    if any(w in speech for w in ["text", "sms", "message", "txt"]):
        resp.say("Ok! You'll get a text shortly. Goodbye!", voice=VOICE)
        resp.hangup()
        background_tasks.add_task(send_opening_sms, caller)
        return HTMLResponse(content=str(resp), media_type="application/xml")

    # ── EMAIL ─────────────────────────────────────────────────────────────────
    if any(w in speech for w in ["email", "e-mail", "mail"]):
        pending_email[call_sid] = {"caller": caller}
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={safe_sid}&caller={safe_caller}",
            method="POST",
            speech_timeout="auto",
            timeout=10,
            language="en-US",
        )
        gather.say("What is your email address?", voice=VOICE)
        resp.append(gather)
        resp.redirect(
            f"/collect-email?call_sid={safe_sid}&caller={safe_caller}&timeout=1",
            method="POST",
        )
        return HTMLResponse(content=str(resp), media_type="application/xml")

    # ── Didn't understand ─────────────────────────────────────────────────────
    gather = Gather(
        input="speech",
        action=f"/choose-channel?call_sid={safe_sid}&caller={safe_caller}",
        method="POST",
        speech_timeout="auto",
        timeout=6,
    )
    gather.say("Sorry, I didn't catch that. Please say email or text.", voice=VOICE)
    resp.append(gather)
    return HTMLResponse(content=str(resp), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 3: collect email address
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/collect-email", methods=["GET", "POST"])
async def collect_email(request: Request, background_tasks: BackgroundTasks):
    form       = await request.form()
    speech     = form.get("SpeechResult", "").strip()
    call_sid   = unquote(request.query_params.get("call_sid", "unknown"))
    caller     = unquote(request.query_params.get("caller",   "Unknown"))
    safe_sid    = quote(call_sid, safe="")
    safe_caller = quote(caller,   safe="")
    is_timeout  = request.query_params.get("timeout", "0") == "1"
    print(f"📧 [{call_sid[:8]}] Email speech: '{speech}'")

    resp = VoiceResponse()

    if is_timeout or not speech:
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={safe_sid}&caller={safe_caller}",
            method="POST",
            speech_timeout="auto",
            timeout=10,
        )
        gather.say(
            "I didn't hear anything. "
            "Please say your email address, for example: john at gmail dot com.",
            voice=VOICE,
        )
        resp.append(gather)
        resp.redirect(
            f"/collect-email?call_sid={safe_sid}&caller={safe_caller}&timeout=1",
            method="POST",
        )
        return HTMLResponse(content=str(resp), media_type="application/xml")

    email_addr = extract_email(speech)

    if email_addr:
        print(f"✅ Email captured: {email_addr}")
        resp.say("Got it! Sending you an email now. Goodbye!", voice=VOICE)
        resp.hangup()
        background_tasks.add_task(send_opening_email, email_addr)
        pending_email.pop(call_sid, None)
    else:
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={safe_sid}&caller={safe_caller}",
            method="POST",
            speech_timeout="auto",
            timeout=10,
        )
        gather.say(
            "Sorry, I didn't catch that. "
            "Please say your email slowly, for example: john at gmail dot com.",
            voice=VOICE,
        )
        resp.append(gather)
        resp.redirect(
            f"/collect-email?call_sid={safe_sid}&caller={safe_caller}&timeout=1",
            method="POST",
        )

    return HTMLResponse(content=str(resp), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# SMS — AI conversation
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-sms", methods=["GET", "POST"])
async def incoming_sms(request: Request):
    form   = await request.form()
    phone  = form.get("From", "")
    body   = form.get("Body", "").strip()
    print(f"💬 SMS from {phone}: {body}")

    now = datetime.now()
    for k in [k for k, t in list(sms_last_active.items())
              if now - t > timedelta(minutes=30)]:
        sms_sessions.pop(k, None)
        sms_last_active.pop(k, None)

    if phone not in sms_sessions:
        sms_sessions[phone] = [{"role": "system", "content": SMS_SYSTEM}]
    sms_last_active[phone] = now
    sms_sessions[phone].append({"role": "user", "content": body})

    reply = call_openai(sms_sessions[phone], max_tokens=300)
    if reply:
        sms_sessions[phone].append({"role": "assistant", "content": reply})
    else:
        reply = "Sorry, I'm having a brief issue. Please try again! — Yann's AI Support"

    twiml = MessagingResponse()
    twiml.message(reply)
    return PlainTextResponse(content=str(twiml), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL INBOX POLLER
# Checks Gmail every EMAIL_POLL_INTERVAL seconds for new replies.
# Replies to any email that:
#   1. Is NOT automated/bulk (no List-Unsubscribe, Precedence: bulk, etc.)
#   2. HAS an In-Reply-To header (meaning it's a reply to something, not
#      a cold/fresh email like a newsletter or promotion)
# ══════════════════════════════════════════════════════════════════════════════

async def email_inbox_poller():
    await asyncio.sleep(3)
    while True:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _check_inbox)
        except Exception as e:
            print(f"❌ Poller error: {e}")
        await asyncio.sleep(EMAIL_POLL_INTERVAL)


def _check_inbox():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(SMTP_USER, SMTP_PASS)
        mail.select("INBOX")

        _, data = mail.uid("search", None, "UNSEEN")
        uids = data[0].split() if data[0] else []
        if uids:
            print(f"🔍 {len(uids)} unseen email(s)")

        for uid in uids:
            uid_str = uid.decode()
            if uid_str in email_processed:
                continue

            _, raw_data = mail.uid("fetch", uid, "(RFC822)")
            if not raw_data or not raw_data[0]:
                continue
            msg = email_lib.message_from_bytes(raw_data[0][1])

            # Parse sender
            sender = _parse_addr(msg.get("From", ""))
            if not sender or sender.lower() == SMTP_USER.lower():
                email_processed.add(uid_str)
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            # Guard 1 — skip automated / bulk / marketing email
            if _is_automated(msg, sender):
                print(f"⏭️  {sender} — automated/bulk, skipping")
                email_processed.add(uid_str)
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            # Guard 2 — must be a reply (has In-Reply-To header)
            # Fresh marketing emails and newsletters never have In-Reply-To.
            # Every genuine human reply does.
            in_reply_to = msg.get("In-Reply-To", "").strip()
            if not in_reply_to:
                print(f"⏭️  {sender} — no In-Reply-To, skipping")
                email_processed.add(uid_str)
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            # Extract body
            body = _extract_body(msg)
            if not body:
                email_processed.add(uid_str)
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            subject    = _decode_header_str(msg.get("Subject", ""))
            reply_subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
            print(f"📨 Reply from {sender}: {body[:80].replace(chr(10),' ')}")

            # Build/continue conversation
            now = datetime.now()
            for k in [k for k, t in list(email_last_active.items())
                      if now - t > timedelta(minutes=60)]:
                email_sessions.pop(k, None)
                email_last_active.pop(k, None)

            if sender not in email_sessions:
                email_sessions[sender] = [{"role": "system", "content": EMAIL_SYSTEM}]
            email_last_active[sender] = now
            email_sessions[sender].append({"role": "user", "content": body})

            ai_reply = call_openai(email_sessions[sender], max_tokens=600)
            if ai_reply:
                email_sessions[sender].append({"role": "assistant", "content": ai_reply})
            else:
                ai_reply = (
                    "I'm having a brief technical issue — please reply again!\n\n"
                    "Best regards,\nYann's AI Support"
                )

            _send_reply(
                to_email    = sender,
                subject     = reply_subj,
                body        = ai_reply,
                in_reply_to = msg.get("Message-ID", ""),
                references  = msg.get("References", "") or msg.get("Message-ID", ""),
            )

            mail.uid("store", uid, "+FLAGS", "\\Seen")
            email_processed.add(uid_str)
            print(f"✅ Reply sent → {sender}")

        mail.logout()

    except imaplib.IMAP4.error as e:
        print(f"❌ IMAP error: {e}")
    except Exception as e:
        print(f"❌ Inbox error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# OUTBOUND MESSAGES
# ══════════════════════════════════════════════════════════════════════════════

async def send_opening_sms(to_number: str):
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
        print(f"❌ SMS error: {e}")


async def send_opening_email(to_email: str):
    if not SMTP_USER or not SMTP_PASS:
        print("⚠️  SMTP not configured")
        return

    body = (
        "Hey there!\n\n"
        "This is Yann's AI Support — you just called us and chose email.\n\n"
        "What do you need my friend? 😊\n\n"
        "Just reply to this email with your question and I'll get back to you "
        "right away with helpful advice on anything you need.\n\n"
        "Best regards,\nYann's AI Support"
    )

    msg               = MIMEMultipart()
    msg["Subject"]    = "Yann's AI Support — We're Ready to Help! 👋"
    msg["From"]       = SMTP_USER
    msg["To"]         = to_email
    msg["Message-ID"] = email_utils.make_msgid(
        domain=SMTP_USER.split("@")[1] if "@" in SMTP_USER else "mail"
    )
    msg.attach(MIMEText(body, "plain"))

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _smtp_send, to_email, msg.as_string())
        print(f"✅ Opening email → {to_email}")
    except Exception as e:
        print(f"❌ Email error: {e}")


def _send_reply(to_email, subject, body, in_reply_to="", references=""):
    domain  = SMTP_USER.split("@")[1] if "@" in SMTP_USER else "mail"
    msg               = MIMEMultipart()
    msg["Subject"]    = subject
    msg["From"]       = SMTP_USER
    msg["To"]         = to_email
    msg["Message-ID"] = email_utils.make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.attach(MIMEText(body, "plain"))
    _smtp_send(to_email, msg.as_string())


def _smtp_send(to_email: str, raw_msg: str):
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, to_email, raw_msg)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _is_automated(msg, sender: str) -> bool:
    if msg.get("List-Unsubscribe") or msg.get("List-ID"):
        return True
    if (msg.get("Precedence") or "").lower().strip() in ("bulk", "list", "junk"):
        return True
    if (msg.get("Auto-Submitted") or "").lower().strip() not in ("", "no"):
        return True
    local = sender.split("@")[0].lower()
    return any(p in local for p in (
        "noreply", "no-reply", "donotreply", "do-not-reply",
        "notifications", "newsletter", "mailer-daemon",
        "postmaster", "bounce",
    ))


def _parse_addr(from_header: str) -> str | None:
    m = re.search(r"<([^>]+)>", from_header)
    addr = m.group(1).strip().lower() if m else from_header.strip().lower()
    return addr if "@" in addr else None


def _decode_header_str(value: str) -> str:
    parts = []
    for raw, charset in _decode_header(value):
        if isinstance(raw, bytes):
            parts.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(raw)
    return "".join(parts)


def _extract_body(msg) -> str:
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == "text/plain"
                    and part.get("Content-Disposition") != "attachment"):
                payload = part.get_payload(decode=True)
                if payload:
                    text = payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )

    clean = []
    for line in text.splitlines():
        if line.strip().startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", line.strip()):
            break
        clean.append(line)
    return "\n".join(clean).strip()


def call_openai(messages: list, max_tokens: int = 300) -> str | None:
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
    t = text.lower().strip()
    t = re.sub(r"\bunderscore\b", "_", t)
    t = re.sub(r"\b(dash|hyphen)\b", "-", t)
    t = re.sub(r"\bperiod\b", ".", t)
    for word, digit in {"zero":"0","one":"1","two":"2","three":"3","four":"4",
                        "five":"5","six":"6","seven":"7","eight":"8","nine":"9"}.items():
        t = re.sub(rf"\b{word}\b", digit, t)
    t = re.sub(r"\s+at\s+",  "@", t)
    t = re.sub(r"\s+dot\s+", ".", t)
    t = re.sub(r"\s+",       "",  t)
    m = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", t)
    return m.group(0) if m else None


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), reload=False)
