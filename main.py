import asyncio
import email as email_lib
import email.utils as email_utils
import imaplib
import json
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
# Get a free key at https://openweathermap.org/api
WEATHER_API_KEY     = os.environ.get("WEATHER_API_KEY", "")

VOICE = "Polly.Joanna"

SMS_SYSTEM = """You are a highly knowledgeable AI assistant for Yann's AI Support.
Help anyone who messages you with accurate, helpful answers on ANY topic — personal, business, technical, creative, emotional, anything.
Be warm, genuine, and conversational. Give real answers, not vague responses.
Keep replies concise for SMS but don't sacrifice accuracy for brevity.
Sign every reply with: — Yann's AI Support"""

def _build_email_system():
    today = datetime.now().strftime("%B %d, %Y")
    return f"""You are a highly knowledgeable AI assistant for Yann's AI Support.
Today's date is {today}. Always use this as your reference for current events and time-sensitive questions.
Help anyone who emails you with thorough, detailed, accurate answers on ANY topic — personal, business, technical, creative, emotional, anything.
Give complete answers like ChatGPT would. Don't cut yourself short. If a question deserves a detailed explanation, give one.
Use clear formatting: bullet points, numbered steps, or sections where helpful.
Be warm, genuine, and conversational — not robotic.
For current events or real-time info, use the web_search tool to look it up before answering.
If you don't know something, say so honestly.
Sign every reply with:
Best regards,
Yann's AI Support"""

def _build_sms_system():
    today = datetime.now().strftime("%B %d, %Y")
    return f"""You are a highly knowledgeable AI assistant for Yann's AI Support.
Today's date is {today}. Always use this as your reference for current events and time-sensitive questions.
Help anyone who messages you with accurate, helpful answers on ANY topic — personal, business, technical, creative, emotional, anything.
Be warm, genuine, and conversational. Give real answers, not vague responses.
For current events or real-time info, use the web_search tool to look it up before answering.
Keep replies concise for SMS but don't sacrifice accuracy for brevity.
Sign every reply with: — Yann's AI Support"""

EMAIL_SYSTEM = _build_email_system()
SMS_SYSTEM   = _build_sms_system()

# ─── Session memory ───────────────────────────────────────────────────────────
sms_sessions:      dict = {}  # phone → [messages]
sms_last_active:   dict = {}  # phone → datetime
email_sessions:    dict = {}  # email → [messages]
email_last_active: dict = {}  # email → datetime
email_processed:   set  = set()  # IMAP UIDs already handled this session
pending_email:     dict = {}  # call_sid → {"caller": "+1..."}
sentreplyids:      dict = {}  # message_id → recipient_email


# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return {
        "status":  "ok",
        "service": "Yann's AI Support",
        "openai":  f"set ({OPENAI_API_KEY[:8]}...)" if OPENAI_API_KEY else "MISSING ❌",
        "twilio":  "set" if TWILIO_SID else "MISSING ❌",
        "smtp":    "set" if SMTP_USER else "MISSING ❌",
        "weather": "set" if WEATHER_API_KEY else "not configured (optional)",
    }


# ─── Diagnostic test endpoint ─────────────────────────────────────────────────
@app.get("/test")
async def test_all(to: str = ""):
    results = {}
    results["env"] = {
        "OPENAI_API_KEY": "✅ set" if OPENAI_API_KEY else "❌ MISSING",
        "TWILIO_SID":     "✅ set" if TWILIO_SID     else "❌ MISSING",
        "SMTP_USER":      SMTP_USER if SMTP_USER      else "❌ MISSING",
        "SMTP_PASS":      "✅ set"  if SMTP_PASS      else "❌ MISSING",
        "IMAP_HOST":      IMAP_HOST,
        "WEATHER_API_KEY": "✅ set" if WEATHER_API_KEY else "not set (optional)",
    }

    if SMTP_USER and SMTP_PASS and to:
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
            results["smtp"] = "✅ login OK"
            msg               = MIMEMultipart()
            msg["Subject"]    = "Yann's AI Support — Test Email"
            msg["From"]       = SMTP_USER
            msg["To"]         = to
            msg["Message-ID"] = email_utils.make_msgid(
                domain=SMTP_USER.split("@")[1] if "@" in SMTP_USER else "mail"
            )
            msg.attach(MIMEText(
                "Hey there!\n\nThis is Yann's AI Support.\n\n"
                "What do you need my friend? 😊\n\n"
                "Just reply to this email with your question!\n\n"
                "Best regards,\nYann's AI Support",
                "plain"
            ))
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _smtp_send, to, msg.as_string())
            results["smtp_send"] = f"✅ test email sent to {to}"
        except Exception as e:
            results["smtp"] = f"❌ {e}"
    elif not to:
        results["smtp"] = "⚠️  add ?to=your@email.com to send a test email"
    else:
        results["smtp"] = "❌ SMTP_USER or SMTP_PASS not set"

    if SMTP_USER and SMTP_PASS:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            mail.login(SMTP_USER, SMTP_PASS)
            mail.select("INBOX")
            _, data = mail.uid("search", None, "UNSEEN")
            unseen = len(data[0].split()) if data[0] else 0
            mail.logout()
            results["imap"] = f"✅ connected — {unseen} unseen email(s) in inbox"
        except Exception as e:
            results["imap"] = f"❌ {e}"
    else:
        results["imap"] = "❌ SMTP_USER or SMTP_PASS not set"

    if OPENAI_API_KEY:
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      "gpt-4o",
                    "messages":   [{"role": "user", "content": "Say OK"}],
                    "max_tokens": 5,
                },
                timeout=15,
            )
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"].strip()
            results["openai"] = f"✅ {reply}"
        except requests.exceptions.HTTPError:
            results["openai"] = f"❌ HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            results["openai"] = f"❌ {e}"
    else:
        results["openai"] = "❌ OPENAI_API_KEY not set"

    return results


# ─── Startup ──────────────────────────────────────────────────────────────────


# ─── Manual inbox trigger (for testing) ───────────────────────────────────────
@app.get("/trigger-check")
async def trigger_check(background_tasks: BackgroundTasks):
    """Manually fire an inbox scan right now. Use for testing."""
    background_tasks.add_task(_check_inbox_async)
    return {"status": "inbox scan triggered — check Render logs for results"}


async def _check_inbox_async():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _check_inbox)


# ─── Send opening email without calling (for testing) ─────────────────────────
@app.get("/send-opening")
async def send_opening(to: str = "", background_tasks: BackgroundTasks = None):
    if not to:
        return {"error": "Add ?to=your@email.com to the URL"}
    background_tasks.add_task(send_opening_email, to)
    return {"status": f"Opening email sending to {to} — check your inbox in a few seconds"}

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
        timeout=8,
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

    if any(w in speech for w in ["text", "sms", "message", "txt"]):
        resp.say("Ok! You'll get a text shortly. Goodbye!", voice=VOICE)
        resp.hangup()
        background_tasks.add_task(send_opening_sms, caller)
        return HTMLResponse(content=str(resp), media_type="application/xml")

    if any(w in speech for w in ["email", "e-mail", "mail"]):
        pending_email[call_sid] = {"caller": caller}
        gather = Gather(
            input="speech",
            # FIX: enhanced model + longer timeout for email capture
            action=f"/collect-email?call_sid={safe_sid}&caller={safe_caller}",
            method="POST",
            speech_timeout=5,          # wait 5s of silence before cutting off
            timeout=15,                # give 15s total to start speaking
            language="en-US",
        )
        gather.say(
            "Please say your email address slowly and clearly. "
            "For example: john at gmail dot com.",
            voice=VOICE,
        )
        resp.append(gather)
        resp.redirect(
            f"/collect-email?call_sid={safe_sid}&caller={safe_caller}&timeout=1",
            method="POST",
        )
        return HTMLResponse(content=str(resp), media_type="application/xml")

    gather = Gather(
        input="speech",
        action=f"/choose-channel?call_sid={safe_sid}&caller={safe_caller}",
        method="POST",
        speech_timeout="auto",
        timeout=8,
    )
    gather.say("Sorry, I didn't catch that. Please say email or text.", voice=VOICE)
    resp.append(gather)
    return HTMLResponse(content=str(resp), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 3: collect email address with confirmation
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
            speech_timeout=5,
            timeout=15,
            language="en-US",
        )
        gather.say(
            "I didn't hear anything. "
            "Please say your email address slowly. "
            "For example: john at gmail dot com.",
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
        # FIX: Read back email letter-by-letter to confirm before sending
        spelled = spell_out_email(email_addr)
        safe_email = quote(email_addr, safe="")
        gather = Gather(
            input="speech",
            action=f"/confirm-email?call_sid={safe_sid}&caller={safe_caller}&email={safe_email}",
            method="POST",
            speech_timeout="auto",
            timeout=8,
            language="en-US",
        )
        gather.say(
            f"I heard {spelled}. "
            "Is that correct? Say yes to confirm or no to try again.",
            voice=VOICE,
        )
        resp.append(gather)
        # If no response, treat as confirmed
        resp.redirect(
            f"/confirm-email?call_sid={safe_sid}&caller={safe_caller}&email={safe_email}&confirmed=1",
            method="POST",
        )
    else:
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={safe_sid}&caller={safe_caller}",
            method="POST",
            speech_timeout=5,
            timeout=15,
            language="en-US",
        )
        gather.say(
            "Sorry, I didn't catch that. "
            "Please say your email slowly. "
            "Spell it out if needed, for example: j o h n at g mail dot com.",
            voice=VOICE,
        )
        resp.append(gather)
        resp.redirect(
            f"/collect-email?call_sid={safe_sid}&caller={safe_caller}&timeout=1",
            method="POST",
        )

    return HTMLResponse(content=str(resp), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 4: confirm email address
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/confirm-email", methods=["GET", "POST"])
async def confirm_email(request: Request, background_tasks: BackgroundTasks):
    form        = await request.form()
    speech      = form.get("SpeechResult", "").strip().lower()
    call_sid    = unquote(request.query_params.get("call_sid",  "unknown"))
    caller      = unquote(request.query_params.get("caller",    "Unknown"))
    email_addr  = unquote(request.query_params.get("email",     ""))
    confirmed   = request.query_params.get("confirmed", "0") == "1"
    safe_sid    = quote(call_sid,   safe="")
    safe_caller = quote(caller,     safe="")

    resp = VoiceResponse()

    # "yes" or timeout fallback (confirmed=1) → send the email
    if confirmed or any(w in speech for w in ["yes", "correct", "right", "yeah", "yep", "sure"]):
        print(f"✅ Email confirmed: {email_addr}")
        resp.say("Got it! Sending you an email now. Goodbye!", voice=VOICE)
        resp.hangup()
        background_tasks.add_task(send_opening_email, email_addr)
        pending_email.pop(call_sid, None)
    elif any(w in speech for w in ["no", "nope", "wrong", "incorrect"]):
        # Go back to collect-email
        safe_email = quote(email_addr, safe="")
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={safe_sid}&caller={safe_caller}",
            method="POST",
            speech_timeout=5,
            timeout=15,
            language="en-US",
        )
        gather.say(
            "No problem. Please say your email address again slowly.",
            voice=VOICE,
        )
        resp.append(gather)
        resp.redirect(
            f"/collect-email?call_sid={safe_sid}&caller={safe_caller}&timeout=1",
            method="POST",
        )
    else:
        # Didn't understand — ask again
        spelled = spell_out_email(email_addr)
        safe_email = quote(email_addr, safe="")
        gather = Gather(
            input="speech",
            action=f"/confirm-email?call_sid={safe_sid}&caller={safe_caller}&email={safe_email}",
            method="POST",
            speech_timeout="auto",
            timeout=8,
        )
        gather.say(
            f"Sorry, say yes if {spelled} is correct, or no to try again.",
            voice=VOICE,
        )
        resp.append(gather)
        resp.redirect(
            f"/confirm-email?call_sid={safe_sid}&caller={safe_caller}&email={safe_email}&confirmed=1",
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
        sms_sessions[phone] = [{"role": "system", "content": _build_sms_system()}]
    sms_last_active[phone] = now
    sms_sessions[phone].append({"role": "user", "content": body})

    # FIX: use tool-calling AI so it can answer weather and location questions
    reply = call_openai_with_tools(sms_sessions[phone], max_tokens=300)
    if reply:
        sms_sessions[phone].append({"role": "assistant", "content": reply})
    else:
        reply = "Sorry, I'm having a brief issue. Please try again! — Yann's AI Support"

    twiml = MessagingResponse()
    twiml.message(reply)
    return PlainTextResponse(content=str(twiml), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL INBOX POLLER
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
    """
    Simple and reliable:
    Search ONLY for replies to emails the bot actually sent.
    No inbox scanning. No UNSEEN flags. No date filters.
    """
    if not sentreplyids:
        return  # Nothing sent yet, nothing to look for

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(SMTP_USER, SMTP_PASS)
        mail.select("INBOX")

        found_any = False
        for sent_mid, recipient in list(sentreplyids.items()):
            # Search specifically for emails replying to this Message-ID
            search_id = sent_mid.strip("<>")
            _, data = mail.uid("search", None, f'HEADER "In-Reply-To" "{sent_mid}"')
            uids = data[0].split() if data[0] else []

            # Also try without angle brackets (some clients strip them)
            if not uids:
                _, data2 = mail.uid("search", None, f'HEADER "In-Reply-To" "{search_id}"')
                uids = data2[0].split() if data2[0] else []

            for uid in uids:
                uid_str = uid.decode()
                if uid_str in email_processed:
                    continue

                found_any = True
                _, raw_data = mail.uid("fetch", uid, "(RFC822)")
                if not raw_data or not raw_data[0]:
                    continue
                msg = email_lib.message_from_bytes(raw_data[0][1])

                sender = _parse_addr(msg.get("From", ""))
                if not sender or sender.lower() == SMTP_USER.lower():
                    email_processed.add(uid_str)
                    continue

                if _is_automated(msg, sender):
                    email_processed.add(uid_str)
                    continue

                body = _extract_body(msg)
                if not body:
                    body = _extract_body_raw(msg)
                if not body:
                    email_processed.add(uid_str)
                    continue

                subject    = _decode_header_str(msg.get("Subject", ""))
                reply_subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
                print(f"📨 Reply from {sender}: {body[:80].replace(chr(10), ' ')}")

                # Build or continue conversation
                if sender not in email_sessions:
                    email_sessions[sender] = [{"role": "system", "content": EMAIL_SYSTEM}]
                email_last_active[sender] = datetime.now()
                email_sessions[sender].append({"role": "user", "content": body})

                ai_reply = call_openai_with_tools(email_sessions[sender], max_tokens=1500)
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

                email_processed.add(uid_str)
                # Remove old message ID, the new reply ID was added by _send_reply
                sentreplyids.pop(sent_mid, None)
                print(f"✅ Replied to {sender}")

        if not found_any:
            pass  # Silent — nothing to reply to yet

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

    mid = email_utils.make_msgid(
        domain=SMTP_USER.split("@")[1] if "@" in SMTP_USER else "mail"
    )
    msg               = MIMEMultipart()
    msg["Subject"]    = "Yann's AI Support — We're Ready to Help! 👋"
    msg["From"]       = SMTP_USER
    msg["To"]         = to_email
    msg["Message-ID"] = mid
    msg.attach(MIMEText(body, "plain"))

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _smtp_send, to_email, msg.as_string())
        # Track this Message-ID so we can find replies to it
        sentreplyids[mid] = to_email
        print(f"✅ Opening email → {to_email} (tracking {mid})")
    except Exception as e:
        print(f"❌ Email error: {e}")


def _send_reply(to_email, subject, body, in_reply_to="", references=""):
    domain  = SMTP_USER.split("@")[1] if "@" in SMTP_USER else "mail"
    mid = email_utils.make_msgid(domain=domain)
    msg               = MIMEMultipart()
    msg["Subject"]    = subject
    msg["From"]       = SMTP_USER
    msg["To"]         = to_email
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.attach(MIMEText(body, "plain"))
    _smtp_send(to_email, msg.as_string())
    # Track this reply so we can find follow-ups to it
    sentreplyids[mid] = to_email


def _smtp_send(to_email: str, raw_msg: str):
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, to_email, raw_msg)


# ══════════════════════════════════════════════════════════════════════════════
# WEATHER & LOCATION TOOLS (for OpenAI function calling)
# ══════════════════════════════════════════════════════════════════════════════

def web_search(query: str) -> str:
    """Search the web using DuckDuckGo — free, no API key needed."""
    try:
        # DuckDuckGo Instant Answer API
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        data = resp.json()

        results = []

        # Abstract (best single answer)
        if data.get("AbstractText"):
            results.append(data["AbstractText"])

        # Answer (e.g. "Barack Obama" for "who is president")
        if data.get("Answer"):
            results.append(data["Answer"])

        # Related topics
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(topic["Text"])

        if results:
            return "\n".join(results)

        # Fallback: DuckDuckGo HTML search for broader queries
        resp2 = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        # Extract first result snippets
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp2.text, "html.parser")
        snippets = [r.get_text(strip=True) for r in soup.select(".result__snippet")[:3]]
        if snippets:
            return "\n".join(snippets)

        return f"No results found for: {query}"
    except Exception as e:
        return f"Search failed: {e}"


# Tool definitions passed to OpenAI
AI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current, real-time information. Use for: current events, who is president, today's news, recent sports scores, stock prices, weather, or anything that may have changed recently.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query, e.g. 'who is the president of the United States 2025'",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city or location. Use when the user asks about weather.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name or city,country code, e.g. 'London' or 'Miami,US'",
                    }
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_location_from_ip",
            "description": "Estimate the caller's approximate location based on IP. Use when user asks where they are.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


def get_weather(location: str) -> str:
    """Call OpenWeatherMap to get current weather."""
    if not WEATHER_API_KEY:
        return "Weather lookup is not configured. Please ask the admin to add a WEATHER_API_KEY."
    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                "q":     location,
                "appid": WEATHER_API_KEY,
                "units": "imperial",  # change to "metric" for Celsius
            },
            timeout=10,
        )
        if resp.status_code == 404:
            return f"I couldn't find weather data for '{location}'. Try a different city name."
        resp.raise_for_status()
        data = resp.json()
        city    = data["name"]
        country = data["sys"]["country"]
        temp    = data["main"]["temp"]
        feels   = data["main"]["feels_like"]
        desc    = data["weather"][0]["description"].capitalize()
        humidity= data["main"]["humidity"]
        return (
            f"Weather in {city}, {country}: {desc}. "
            f"Temperature: {temp:.0f}°F (feels like {feels:.0f}°F). "
            f"Humidity: {humidity}%."
        )
    except Exception as e:
        return f"Sorry, I had trouble fetching the weather: {e}"


def get_location_from_ip() -> str:
    """Use ip-api.com (free, no key needed) to get approximate location."""
    try:
        resp = requests.get("http://ip-api.com/json/", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            city    = data.get("city", "unknown city")
            region  = data.get("regionName", "")
            country = data.get("country", "")
            return (
                f"Based on the server's IP address, the approximate location is "
                f"{city}, {region}, {country}. "
                f"Note: this reflects the server location, not your personal location."
            )
        return "I wasn't able to determine a location from the IP address."
    except Exception as e:
        return f"Sorry, I had trouble getting location info: {e}"


def call_openai_with_tools(messages: list, max_tokens: int = 300) -> str | None:
    """
    Call OpenAI with tool support. If the model wants to call a tool
    (weather / location), execute it and send the result back.
    """
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
                "tools":       AI_TOOLS,
                "tool_choice": "auto",
                "max_tokens":  max_tokens,
                "temperature": 0.75,
            },
            timeout=25,
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]

        # No tool call — return the text directly
        if choice["finish_reason"] != "tool_calls":
            return choice["message"]["content"].strip()

        # Tool call requested
        tool_calls = choice["message"]["tool_calls"]
        # Append assistant's tool-call message to conversation
        messages_with_tools = messages + [choice["message"]]

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = json.loads(tc["function"]["arguments"])
            print(f"🔧 Tool call: {fn_name}({fn_args})")

            if fn_name == "web_search":
                tool_result = web_search(fn_args.get("query", ""))
            elif fn_name == "get_weather":
                tool_result = get_weather(fn_args.get("location", ""))
            elif fn_name == "get_location_from_ip":
                tool_result = get_location_from_ip()
            else:
                tool_result = "Unknown tool."

            messages_with_tools.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      tool_result,
            })

        # Second call with tool results
        resp2 = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "gpt-4o",
                "messages":    messages_with_tools,
                "max_tokens":  max_tokens,
                "temperature": 0.75,
            },
            timeout=25,
        )
        resp2.raise_for_status()
        return resp2.json()["choices"][0]["message"]["content"].strip()

    except requests.exceptions.HTTPError as e:
        print(f"❌ OpenAI HTTP error: {e}")
        return None
    except Exception as e:
        print(f"❌ OpenAI error: {e}")
        return None


# Keep simple call_openai for backward compat
def call_openai(messages: list, max_tokens: int = 300) -> str | None:
    return call_openai_with_tools(messages, max_tokens)


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
    """Extract body, stripping quoted lines."""
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


def _extract_body_raw(msg) -> str:
    """Fallback: extract body WITHOUT stripping quotes (for when body was all-quoted)."""
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
    return text.strip()


def spell_out_email(email_addr: str) -> str:
    """Convert email to spoken form: 'j o h n at gmail dot com'"""
    if "@" not in email_addr:
        return email_addr
    local, domain = email_addr.split("@", 1)
    # spell out local part letter by letter
    local_spoken = " ".join(local)
    # make domain readable
    domain_spoken = domain.replace(".", " dot ")
    return f"{local_spoken}, at, {domain_spoken}"


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
    t = re.sub(r"\s+",        "",  t)
    m = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", t)
    return m.group(0) if m else None


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), reload=False)
