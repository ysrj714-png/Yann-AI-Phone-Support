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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TWILIO_SID     = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER  = os.environ.get("TWILIO_PHONE_NUMBER", "+18449704843")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER      = os.environ.get("SMTP_USER", "")
SMTP_PASS      = os.environ.get("SMTP_PASS", "")
IMAP_HOST      = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT      = int(os.environ.get("IMAP_PORT", 993))
# How often (seconds) to check inbox for new replies
EMAIL_POLL_INTERVAL = int(os.environ.get("EMAIL_POLL_INTERVAL", 30))
# Where to persist sent Message-IDs so they survive server restarts
SENT_IDS_FILE = os.environ.get("SENT_IDS_FILE", "sent_message_ids.json")

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

# ─── SMS session memory ───────────────────────────────────────────────────────
sms_sessions:    dict = {}   # phone_number → [messages]
sms_last_active: dict = {}   # phone_number → datetime

# ─── Email session memory ─────────────────────────────────────────────────────
email_sessions:    dict = {}  # sender_email → [messages]
email_last_active: dict = {}  # sender_email → datetime
# UIDs already handled — prevents double-processing within a session
email_processed: set = set()
# Message-IDs of emails the bot has sent — we ONLY reply to emails whose
# In-Reply-To matches one of these. This stops the bot from replying to
# marketing emails, newsletters, or anything else in the inbox.
# PERSISTED to disk so server restarts don't lose track of past sent emails.
sent_message_ids: set  = set()
# Set to True after the first poll drains the backlog without replying
email_startup_done: bool = False


def _load_sent_ids():
    """Load sent Message-IDs from disk into memory on startup."""
    global sent_message_ids
    try:
        if os.path.exists(SENT_IDS_FILE):
            with open(SENT_IDS_FILE, "r") as f:
                ids = json.load(f)
            sent_message_ids.update(ids)
            print(f"📂 Loaded {len(ids)} sent Message-ID(s) from {SENT_IDS_FILE}")
        else:
            print(f"📂 No sent-IDs file yet ({SENT_IDS_FILE}) — will create on first send")
    except Exception as e:
        print(f"⚠️  Could not load sent IDs: {e}")


def _persist_sent_id(msg_id: str):
    """Add a Message-ID to the in-memory set AND write it to disk."""
    sent_message_ids.add(msg_id)
    try:
        with open(SENT_IDS_FILE, "w") as f:
            json.dump(list(sent_message_ids), f)
    except Exception as e:
        print(f"⚠️  Could not persist sent ID: {e}")

# ─── Pending email collection (from phone call) ───────────────────────────────
pending_email: dict = {}     # call_sid → {"caller": "+1...", "stage": "collecting"}


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return {
        "status":     "ok",
        "service":    "Yann's AI Support",
        "openai_key": f"set ({OPENAI_API_KEY[:8]}...)" if OPENAI_API_KEY else "MISSING ❌",
        "twilio":     "set" if TWILIO_SID else "MISSING ❌",
        "smtp":       "set" if SMTP_USER else "MISSING ❌",
    }


# ─── Startup: launch email inbox poller ───────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    # Reload sent Message-IDs from disk FIRST so the poller can
    # recognise replies to emails sent before this server restart.
    _load_sent_ids()
    if SMTP_USER and SMTP_PASS:
        asyncio.create_task(email_inbox_poller())
        print(f"📬 Email inbox poller started (every {EMAIL_POLL_INTERVAL}s) for {SMTP_USER}")
    else:
        print("⚠️  SMTP_USER/SMTP_PASS not set — email reply polling disabled")


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL INBOX POLLER — checks Gmail via IMAP and replies with AI
# ══════════════════════════════════════════════════════════════════════════════

async def email_inbox_poller():
    """Async loop: poll Gmail inbox every EMAIL_POLL_INTERVAL seconds."""
    # Wait a moment for server to finish starting up
    await asyncio.sleep(5)
    while True:
        try:
            loop = asyncio.get_event_loop()
            # Run the blocking IMAP calls in a thread so the event loop isn't blocked
            await loop.run_in_executor(None, _check_and_reply_inbox)
        except Exception as e:
            print(f"❌ Email poller error: {e}")
        await asyncio.sleep(EMAIL_POLL_INTERVAL)


def _check_and_reply_inbox():
    """
    Synchronous: connect to Gmail via IMAP, find UNSEEN emails,
    generate AI replies, send them, and mark originals as read.

    Guard rails (in order):
      1. First-run drain  — on startup, skip ALL existing unread emails
                            so the bot doesn't reply to your whole backlog.
      2. In-Reply-To check — only reply to emails whose In-Reply-To / References
                            header matches a Message-ID the bot itself sent.
                            This means marketing emails, newsletters, and random
                            incoming mail are NEVER replied to.
      3. Automated filter — extra safety net: drop bulk/list/noreply emails.
    """
    global email_startup_done

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(SMTP_USER, SMTP_PASS)
        mail.select("INBOX")

        # Fetch all UNSEEN email UIDs
        _, data = mail.uid("search", None, "UNSEEN")
        uids = data[0].split() if data[0] else []

        # ── First-run: drain backlog ───────────────────────────────────────────
        # On startup there may be thousands of old unread emails. Add all their
        # UIDs to email_processed so they're never touched again this session,
        # then return without replying to any of them.
        if not email_startup_done:
            for uid in uids:
                email_processed.add(uid.decode())
            email_startup_done = True
            print(f"📬 Inbox initialized — {len(uids)} existing unread email(s) skipped (backlog drain)")
            mail.logout()
            return

        for uid in uids:
            uid_str = uid.decode()
            if uid_str in email_processed:
                continue

            # Fetch full message
            _, msg_data = mail.uid("fetch", uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            # ── Parse sender ──────────────────────────────────────────────────
            from_raw    = msg.get("From", "")
            sender_addr = _parse_addr(from_raw)
            if not sender_addr:
                print(f"⚠️  Could not parse sender: {from_raw!r} — skipping")
                email_processed.add(uid_str)
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            # Never reply to ourselves
            if sender_addr.lower() == SMTP_USER.lower():
                email_processed.add(uid_str)
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            # ── Guard 1: must be a reply to one of our emails ─────────────────
            # Check In-Reply-To and References headers for a Message-ID we sent.
            in_reply_to = msg.get("In-Reply-To", "").strip()
            references  = msg.get("References",  "").strip()
            combined_refs = f"{in_reply_to} {references}"

            is_reply_to_bot = any(
                mid and mid in combined_refs
                for mid in sent_message_ids
            )
            if not is_reply_to_bot:
                print(f"⏭️  Skipping {sender_addr} — not a reply to one of our emails")
                email_processed.add(uid_str)
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            # ── Guard 2: automated/marketing email filter ─────────────────────
            if _is_automated_email(msg, sender_addr):
                print(f"⏭️  Skipping {sender_addr} — looks automated/bulk")
                email_processed.add(uid_str)
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            # ── Parse subject ────────────────────────────────────────────────
            subject_raw = msg.get("Subject", "No Subject")
            subject     = _decode_mime_words(subject_raw)
            reply_subj  = subject if subject.lower().startswith("re:") else f"Re: {subject}"

            # ── Parse body ───────────────────────────────────────────────────
            body = _extract_text_body(msg)
            if not body:
                print(f"📭 Empty body from {sender_addr} — skipping")
                email_processed.add(uid_str)
                mail.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            print(f"📨 Reply from {sender_addr} | Subject: {subject!r}")
            print(f"   Body preview: {body[:120].replace(chr(10), ' ')}")

            # ── Build / continue conversation ────────────────────────────────
            now = datetime.now()
            for k in [k for k, t in list(email_last_active.items())
                      if now - t > timedelta(minutes=60)]:
                email_sessions.pop(k, None)
                email_last_active.pop(k, None)

            if sender_addr not in email_sessions:
                email_sessions[sender_addr] = [
                    {"role": "system", "content": EMAIL_SYSTEM}
                ]
            email_last_active[sender_addr] = now
            email_sessions[sender_addr].append({"role": "user", "content": body})

            # ── Call OpenAI ───────────────────────────────────────────────────
            ai_reply = call_openai(email_sessions[sender_addr], max_tokens=600)
            if ai_reply:
                email_sessions[sender_addr].append(
                    {"role": "assistant", "content": ai_reply}
                )
            else:
                ai_reply = (
                    "I'm having a brief technical issue — please reply again in a moment!\n\n"
                    "Best regards,\nYann's AI Support"
                )

            # ── Send reply ────────────────────────────────────────────────────
            msg_id     = msg.get("Message-ID", "")
            references = msg.get("References", "") or msg_id
            _send_email_reply(
                to_email    = sender_addr,
                subject     = reply_subj,
                body        = ai_reply,
                in_reply_to = msg_id,
                references  = references,
            )

            mail.uid("store", uid, "+FLAGS", "\\Seen")
            email_processed.add(uid_str)
            print(f"✅ Reply sent → {sender_addr}")

        mail.logout()

    except imaplib.IMAP4.error as e:
        print(f"❌ IMAP auth/connection error: {e}")
    except Exception as e:
        print(f"❌ Inbox check error: {e}")


# ─── Email helpers ─────────────────────────────────────────────────────────────

def _is_automated_email(msg, sender_addr: str) -> bool:
    """
    Return True if this email looks automated, bulk, or marketing.
    Used as a secondary safety net — the primary guard is the
    In-Reply-To / sent_message_ids check.
    """
    # List-Unsubscribe is the clearest signal of a mailing-list / marketing email
    if msg.get("List-Unsubscribe") or msg.get("List-ID"):
        return True
    # Precedence header
    precedence = (msg.get("Precedence") or "").lower().strip()
    if precedence in ("bulk", "list", "junk"):
        return True
    # Auto-Submitted header (auto-replies, out-of-office, delivery reports)
    auto_sub = (msg.get("Auto-Submitted") or "").lower().strip()
    if auto_sub and auto_sub != "no":
        return True
    # X-Mailer hints for bulk platforms
    x_mailer = (msg.get("X-Mailer") or "").lower()
    if any(k in x_mailer for k in ("mailchimp", "sendgrid", "marketo", "salesforce", "hubspot")):
        return True
    # Sender address patterns that indicate automated mail
    local = sender_addr.split("@")[0].lower()
    auto_patterns = (
        "noreply", "no-reply", "donotreply", "do-not-reply",
        "notifications", "newsletter", "mailer-daemon",
        "postmaster", "bounce", "automailer",
    )
    if any(p in local for p in auto_patterns):
        return True
    return False

def _decode_mime_words(s: str) -> str:
    """Decode RFC 2047 encoded email header words (e.g. =?UTF-8?B?...?=)."""
    parts = []
    for raw, charset in _decode_header(s):
        if isinstance(raw, bytes):
            parts.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(raw)
    return "".join(parts)


def _parse_addr(from_header: str) -> str | None:
    """
    Extract a plain email address from a From header like:
      'John Doe <john@example.com>'  →  'john@example.com'
      'john@example.com'             →  'john@example.com'
    """
    match = re.search(r"<([^>]+)>", from_header)
    if match:
        return match.group(1).strip().lower()
    # Fallback: the whole header might just be an address
    addr = from_header.strip().lower()
    return addr if "@" in addr else None


def _extract_text_body(msg) -> str:
    """
    Pull the plaintext body out of an email.Message object.
    Strips quoted-reply lines (lines starting with >) so the AI
    only sees the new message, not the entire thread history.
    """
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               part.get("Content-Disposition") != "attachment":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")

    # Strip quoted lines ("> ..." ) and common "On ... wrote:" lines
    clean_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", stripped):
            break  # Everything after this is a quote
        clean_lines.append(line)

    return "\n".join(clean_lines).strip()


def _send_email_reply(
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
):
    """Send an email reply via SMTP, threading it to the original message.
    The generated Message-ID is saved to sent_message_ids so that when
    the recipient replies, the poller recognises it as a legitimate conversation.
    """
    # Generate a unique Message-ID for this outgoing email
    domain  = SMTP_USER.split("@")[1] if "@" in SMTP_USER else "mail"
    new_mid = email_utils.make_msgid(domain=domain)

    msg                = MIMEMultipart()
    msg["Subject"]     = subject
    msg["From"]        = SMTP_USER
    msg["To"]          = to_email
    msg["Message-ID"]  = new_mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, to_email, msg.as_string())

    # Track this Message-ID so replies to it are recognised — persisted to disk
    # so the mapping survives server restarts.
    _persist_sent_id(new_mid)


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Step 1: Answer and ask text or email
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    form     = await request.form()
    call_sid = form.get("CallSid", "unknown")
    caller   = form.get("From", "Unknown")
    print(f"📞 Call from {caller} | SID: {call_sid}")

    # URL-encode caller so the + in +1XXXXXXXXXX doesn't become a space
    safe_caller   = quote(caller, safe="")
    safe_call_sid = quote(call_sid, safe="")

    response = VoiceResponse()
    gather   = Gather(
        input="speech",
        action=f"/choose-channel?call_sid={safe_call_sid}&caller={safe_caller}",
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
    # unquote decodes %2B back to + so we have the real phone number
    call_sid = unquote(request.query_params.get("call_sid", "unknown"))
    caller   = unquote(request.query_params.get("caller", "Unknown"))
    safe_caller   = quote(caller, safe="")
    safe_call_sid = quote(call_sid, safe="")
    print(f"📲 [{call_sid[:8]}] Channel choice: '{speech}'")

    response = VoiceResponse()

    # ── TEXT chosen ───────────────────────────────────────────────────────────
    if any(w in speech for w in ["text", "sms", "message", "txt"]):
        response.say(
            "Ok, transferring to text now. You will receive a message shortly. Goodbye!",
            voice=VOICE,
        )
        response.hangup()
        background_tasks.add_task(send_opening_sms, caller)
        return HTMLResponse(content=str(response), media_type="application/xml")

    # ── EMAIL chosen ──────────────────────────────────────────────────────────
    if any(w in speech for w in ["email", "e-mail", "mail"]):
        pending_email[call_sid] = {"caller": caller}
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={safe_call_sid}&caller={safe_caller}",
            method="POST",
            speech_timeout="auto",
            timeout=10,
            language="en-US",
        )
        gather.say("What is your email address?", voice=VOICE)
        response.append(gather)
        response.redirect(
            f"/collect-email?call_sid={safe_call_sid}&caller={safe_caller}&timeout=1",
            method="POST",
        )
        return HTMLResponse(content=str(response), media_type="application/xml")

    # ── Couldn't understand — ask again ───────────────────────────────────────
    gather = Gather(
        input="speech",
        action=f"/choose-channel?call_sid={safe_call_sid}&caller={safe_caller}",
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
    call_sid = unquote(request.query_params.get("call_sid", "unknown"))
    caller   = unquote(request.query_params.get("caller", "Unknown"))
    safe_caller   = quote(caller, safe="")
    safe_call_sid = quote(call_sid, safe="")
    is_timeout = request.query_params.get("timeout", "0") == "1"
    print(f"📧 [{call_sid[:8]}] Email speech: '{speech}'")

    response = VoiceResponse()

    # If this was a timeout redirect (Gather expired with no input), re-prompt.
    if is_timeout or not speech:
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={safe_call_sid}&caller={safe_caller}",
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
            f"/collect-email?call_sid={safe_call_sid}&caller={safe_caller}&timeout=1",
            method="POST",
        )
        return HTMLResponse(content=str(response), media_type="application/xml")

    email = extract_email(speech)

    if email:
        print(f"✅ Email captured: {email}")
        response.say("Got it! Sending you an email now. Goodbye!", voice=VOICE)
        response.hangup()
        background_tasks.add_task(send_opening_email, email)
        pending_email.pop(call_sid, None)
    else:
        # Couldn't parse the address — try again with a helpful example
        gather = Gather(
            input="speech",
            action=f"/collect-email?call_sid={safe_call_sid}&caller={safe_caller}",
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
            f"/collect-email?call_sid={safe_call_sid}&caller={safe_caller}&timeout=1",
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
    """Send the first email after caller gives their address.
    Saves the outgoing Message-ID to sent_message_ids so the poller
    knows to accept replies from this person.
    """
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

    domain  = SMTP_USER.split("@")[1] if "@" in SMTP_USER else "mail"
    new_mid = email_utils.make_msgid(domain=domain)

    msg               = MIMEMultipart()
    msg["Subject"]    = subject
    msg["From"]       = SMTP_USER
    msg["To"]         = to_email
    msg["Message-ID"] = new_mid
    msg.attach(MIMEText(body, "plain"))

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _smtp_send_raw, to_email, msg)
        # Track so the poller accepts replies from this person — persisted to disk
    _persist_sent_id(new_mid)
    print(f"✅ Opening email → {to_email} (Message-ID: {new_mid})")
    except Exception as e:
        print(f"❌ Opening email error: {e}")


def _smtp_send_raw(to_email: str, msg: MIMEMultipart):
    """Synchronous SMTP send — called via run_in_executor."""
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, to_email, msg.as_string())


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
