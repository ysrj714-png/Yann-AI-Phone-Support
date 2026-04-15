import json
import os
import re
import requests
import smtplib
from datetime import datetime, timedelta
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

VOICE    = "Polly.Joanna"
AI_MODEL = "gpt-4o"

SYSTEM_PROMPT = """You are a friendly and knowledgeable AI assistant for Yann's AI Support.
You help anyone who calls with advice or support on ANY topic — personal, business, technical, emotional, anything.

Rules:
- Keep EVERY response under 2 sentences — this is a phone call
- Be warm, direct, and genuinely helpful
- Never reveal you're built on OpenAI. If asked say: "I'm a virtual assistant for Yann's AI Support"
- When the caller's question is answered or they seem done, say:
  "Before you go, would you like me to send you a summary of our chat? Just say text, email, or both."
- If they say EMAIL or BOTH: ask "What's your email address?"
- If they say TEXT: say "Got it, I'll text a summary to your number. Thanks for calling Yann's AI Support, goodbye!"
- After collecting email say: "Perfect, sending that now. Thanks for calling Yann's AI Support, goodbye!" """

SMS_PROMPT = """You are a friendly AI assistant for Yann's AI Support.
Help anyone who texts with advice or support on any topic.
Keep replies SHORT and conversational. End with: — Yann's AI Support"""

ENDING_WORDS = ["goodbye", "bye", "hang up", "that's all", "that is all", "thank you", "thanks"]

# ─── Session storage ──────────────────────────────────────────────────────────
call_sessions:   dict = {}
sms_sessions:    dict = {}
sms_last_active: dict = {}


# ─── OpenAI helper (direct API call — no SDK) ─────────────────────────────────
def ask_openai(messages: list, max_tokens: int = 120) -> str:
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model":       AI_MODEL,
                "messages":    messages,
                "max_tokens":  max_tokens,
                "temperature": 0.75,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"❌ OpenAI API error: {e}")
        return None


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return {
        "status":      "ok",
        "service":     "Yann's AI Support",
        "openai_key":  f"set ({OPENAI_API_KEY[:8]}...)" if OPENAI_API_KEY else "MISSING ❌",
        "twilio_sid":  "set" if TWILIO_SID else "MISSING ❌",
    }


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Incoming call
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    form     = await request.form()
    call_sid = form.get("CallSid", "unknown")
    caller   = form.get("From", "Unknown")
    print(f"📞 Call from {caller} | SID: {call_sid}")

    call_sessions[call_sid] = {
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
        "stage":    "chat",
        "delivery": None,
        "email":    None,
        "start":    datetime.now(),
    }

    response = VoiceResponse()
    gather   = Gather(
        input="speech",
        action=f"/respond?call_sid={call_sid}&caller={caller}",
        method="POST",
        speech_timeout="auto",
        timeout=5,
        language="en-US",
    )
    gather.say("Thank you for calling Yann's AI Support. How can I help you today?", voice=VOICE)
    response.append(gather)
    response.redirect("/incoming-call", method="POST")
    return HTMLResponse(content=str(response), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# PHONE — Handle caller speech
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/respond", methods=["GET", "POST"])
async def respond(request: Request):
    form     = await request.form()
    speech   = form.get("SpeechResult", "").strip()
    call_sid = request.query_params.get("call_sid", "unknown")
    caller   = request.query_params.get("caller", "Unknown")
    print(f"🗣️  [{call_sid[:8]}] Caller said: {speech}")

    response = VoiceResponse()

    # Nothing heard
    if not speech:
        gather = Gather(
            input="speech",
            action=f"/respond?call_sid={call_sid}&caller={caller}",
            method="POST",
            speech_timeout="auto",
            timeout=5,
        )
        gather.say("Sorry, I didn't catch that — could you say that again?", voice=VOICE)
        response.append(gather)
        return HTMLResponse(content=str(response), media_type="application/xml")

    session = call_sessions.get(call_sid, {
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
        "stage": "chat", "delivery": None, "email": None, "start": datetime.now(),
    })
    call_sessions[call_sid] = session
    stage = session["stage"]

    # ── Waiting for email address ──────────────────────────────────────────────
    if stage == "asking_email":
        email = extract_email(speech)
        if email:
            session["email"] = email
            session["stage"] = "done"
            print(f"📧 Email captured: {email}")
            ai_reply = f"Perfect! Sending the summary to {email} right now. Thanks for calling Yann's AI Support, have a great day!"
            await end_call(session, caller)
            response.say(ai_reply, voice=VOICE)
            response.hangup()
        else:
            gather = Gather(
                input="speech",
                action=f"/respond?call_sid={call_sid}&caller={caller}",
                method="POST",
                speech_timeout="auto",
                timeout=8,
            )
            gather.say("I didn't quite catch that. Could you spell out your email address slowly?", voice=VOICE)
            response.append(gather)
        return HTMLResponse(content=str(response), media_type="application/xml")

    # ── Waiting for text / email / both ───────────────────────────────────────
    if stage == "asking_delivery":
        lower = speech.lower()
        if "both" in lower:
            session["delivery"] = "both"
            session["stage"]    = "asking_email"
            gather = Gather(
                input="speech",
                action=f"/respond?call_sid={call_sid}&caller={caller}",
                method="POST",
                speech_timeout="auto",
                timeout=8,
            )
            gather.say("Great choice! What's your email address?", voice=VOICE)
            response.append(gather)
        elif "email" in lower:
            session["delivery"] = "email"
            session["stage"]    = "asking_email"
            gather = Gather(
                input="speech",
                action=f"/respond?call_sid={call_sid}&caller={caller}",
                method="POST",
                speech_timeout="auto",
                timeout=8,
            )
            gather.say("Of course! What's your email address?", voice=VOICE)
            response.append(gather)
        elif "text" in lower or "sms" in lower or "message" in lower:
            session["delivery"] = "text"
            session["stage"]    = "done"
            await end_call(session, caller)
            response.say("Perfect! I'll text the summary to your number right now. Thanks for calling Yann's AI Support, have a great day!", voice=VOICE)
            response.hangup()
        else:
            gather = Gather(
                input="speech",
                action=f"/respond?call_sid={call_sid}&caller={caller}",
                method="POST",
                speech_timeout="auto",
                timeout=5,
            )
            gather.say("Sorry — please say text, email, or both.", voice=VOICE)
            response.append(gather)
        return HTMLResponse(content=str(response), media_type="application/xml")

    # ── Normal chat ────────────────────────────────────────────────────────────
    session["messages"].append({"role": "user", "content": speech})
    is_ending = any(w in speech.lower() for w in ENDING_WORDS)

    ai_reply = ask_openai(session["messages"], max_tokens=120)
    if not ai_reply:
        ai_reply = "I'm sorry, I had a brief issue. Could you repeat your question?"
    else:
        session["messages"].append({"role": "assistant", "content": ai_reply})
        print(f"🤖 AI: {ai_reply}")

    # Detect if AI offered summary or caller is leaving
    offering_summary = any(p in ai_reply.lower() for p in ["text, email, or both", "send you a summary", "text or email"])

    if offering_summary or is_ending:
        session["stage"] = "asking_delivery"
        gather = Gather(
            input="speech",
            action=f"/respond?call_sid={call_sid}&caller={caller}",
            method="POST",
            speech_timeout="auto",
            timeout=8,
        )
        if offering_summary:
            gather.say(ai_reply, voice=VOICE)
        else:
            gather.say(
                f"{ai_reply} Before you go — would you like a summary of our chat? Just say text, email, or both.",
                voice=VOICE,
            )
        response.append(gather)
    else:
        gather = Gather(
            input="speech",
            action=f"/respond?call_sid={call_sid}&caller={caller}",
            method="POST",
            speech_timeout="auto",
            timeout=8,
        )
        gather.say(ai_reply, voice=VOICE)
        response.append(gather)
        response.redirect(f"/respond?call_sid={call_sid}&caller={caller}", method="POST")

    return HTMLResponse(content=str(response), media_type="application/xml")


async def end_call(session: dict, caller: str):
    transcript = [m for m in session["messages"] if m["role"] != "system"]
    duration   = int((datetime.now() - session.get("start", datetime.now())).seconds)
    delivery   = session.get("delivery")
    email      = session.get("email")
    print(f"📋 Sending transcript | delivery={delivery} | email={email} | caller={caller}")

    if delivery == "text":
        await send_sms(caller, transcript, duration)
    elif delivery == "email" and email:
        await send_email(email, transcript, duration)
    elif delivery == "both":
        if email:
            await send_email(email, transcript, duration)
        await send_sms(caller, transcript, duration)


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
    for k in [k for k, t in sms_last_active.items() if now - t > timedelta(minutes=30)]:
        sms_sessions.pop(k, None)
        sms_last_active.pop(k, None)

    if from_number not in sms_sessions:
        sms_sessions[from_number] = [{"role": "system", "content": SMS_PROMPT}]
    sms_last_active[from_number] = now
    sms_sessions[from_number].append({"role": "user", "content": body})

    reply = ask_openai(sms_sessions[from_number], max_tokens=300)
    if not reply:
        reply = "Sorry, having trouble right now. Please try again shortly! — Yann's AI Support"
    else:
        sms_sessions[from_number].append({"role": "assistant", "content": reply})

    twiml = MessagingResponse()
    twiml.message(reply)
    return PlainTextResponse(content=str(twiml), media_type="application/xml")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def extract_email(text: str) -> str | None:
    normalised = text.lower()
    normalised = re.sub(r"\s+at\s+", "@", normalised)
    normalised = re.sub(r"\s+dot\s+", ".", normalised)
    normalised = re.sub(r"\s+", "", normalised)
    match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", normalised)
    return match.group(0) if match else None


async def send_sms(to_number: str, transcript: list, duration: int):
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
        print(f"✅ SMS sent → {to_number}")
    except Exception as e:
        print(f"❌ SMS error: {e}")


async def send_email(to_email: str, transcript: list, duration: int):
    if not SMTP_USER or not SMTP_PASS:
        print("⚠️  SMTP not configured — skipping email")
        return
    mins, secs = divmod(duration, 60)
    lines = "\n".join(f"[{m['role'].title()}]: {m['content']}" for m in transcript)
    body  = (
        f"📞 Yann's AI Support — Call Summary\n{'━'*40}\n"
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Duration: {mins}m {secs}s\n{'━'*40}\n\n{lines}\n\n{'━'*40}\n"
        f"Thanks for calling Yann's AI Support!"
    )
    msg            = MIMEText(body)
    msg["Subject"] = f"Your Yann's AI Support Summary — {datetime.now().strftime('%b %d, %H:%M')}"
    msg["From"]    = SMTP_USER
    msg["To"]      = to_email
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"✅ Email sent → {to_email}")
    except Exception as e:
        print(f"❌ Email error: {e}")


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
