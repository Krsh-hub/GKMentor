"""Outgoing email for account verification and password resets. Standard library only.

Pick ONE (checked in this order):
  RESEND_API_KEY=re_...             HTTPS API (works on Render's free tier, which blocks outbound SMTP ports)
  GK_SMTP_HOST / GK_SMTP_PORT (587) / GK_SMTP_USER / GK_SMTP_PASS   any SMTP server, e.g. Gmail with an app password
plus GK_MAIL_FROM="GK Mentor <no-reply@yourdomain>" (Resend needs a verified domain; "onboarding@resend.dev" works for testing).
With nothing set, emails are printed to the server log and kept in OUTBOX (local development and tests)."""
import json, os, smtplib, ssl, sys, threading, urllib.request
from email.message import EmailMessage

OUTBOX = []                                  # messages "sent" while no provider is configured
SYNC = False                                 # tests set this so sending happens before the request returns

def provider():
    if os.getenv("RESEND_API_KEY"): return "resend"
    if os.getenv("GK_SMTP_HOST"): return "smtp"
    return "log"

def _from():
    return os.getenv("GK_MAIL_FROM") or os.getenv("GK_SMTP_USER") or "GK Mentor <onboarding@resend.dev>"

def _send_now(to, subject, text, html):
    p = provider()
    try:
        if p == "resend":
            body = json.dumps({"from": _from(), "to": [to], "subject": subject, "text": text, **({"html": html} if html else {})}).encode()
            req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST",
                                         headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}", "Content-Type": "application/json",
                                                  "User-Agent": "gkmentor/1.0"})
            urllib.request.urlopen(req, timeout=20).read()
        elif p == "smtp":
            m = EmailMessage(); m["From"], m["To"], m["Subject"] = _from(), to, subject
            m.set_content(text)
            if html: m.add_alternative(html, subtype="html")
            port = int(os.getenv("GK_SMTP_PORT", "587"))
            ctx = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(os.environ["GK_SMTP_HOST"], port, context=ctx, timeout=20) as s:
                    s.login(os.environ.get("GK_SMTP_USER", ""), os.environ.get("GK_SMTP_PASS", "")); s.send_message(m)
            else:
                with smtplib.SMTP(os.environ["GK_SMTP_HOST"], port, timeout=20) as s:
                    s.starttls(context=ctx)
                    if os.getenv("GK_SMTP_USER"): s.login(os.environ["GK_SMTP_USER"], os.environ.get("GK_SMTP_PASS", ""))
                    s.send_message(m)
        else:
            OUTBOX.append(dict(to=to, subject=subject, text=text))
            print(f"[mail] no email provider set; would send to {to}: {subject}\n{text}", file=sys.stderr, flush=True)
        return True
    except Exception as e:
        print(f"[mail] FAILED sending '{subject}' to {to} via {p}: {e}", file=sys.stderr, flush=True)
        return False

def send(to, subject, text, html=None):
    """Send in the background so a slow mail server never delays the login page."""
    if SYNC or provider() == "log": return _send_now(to, subject, text, html)
    threading.Thread(target=_send_now, args=(to, subject, text, html), daemon=True).start()
    return True

def _wrap(title, intro, link, label, outro):
    text = f"{title}\n\n{intro}\n\n{link}\n\n{outro}\n\n- GK Mentor"
    html = (f'<div style="font-family:system-ui,sans-serif;max-width:520px;margin:auto;padding:24px;color:#111">'
            f'<h2 style="margin:0 0 12px">{title}</h2><p>{intro}</p>'
            f'<p><a href="{link}" style="display:inline-block;background:#e0a52c;color:#1a1405;padding:12px 18px;border-radius:8px;'
            f'text-decoration:none;font-weight:700">{label}</a></p><p style="color:#555;font-size:13px">Or paste this link: {link}</p>'
            f'<p style="color:#555;font-size:13px">{outro}</p></div>')
    return text, html

def verification(to, name, link):
    t, h = _wrap(f"Hi {name}, confirm your email", "Click the button to confirm this email address for your GK Mentor account.",
                 link, "Confirm email", "The link works for 48 hours. If you did not sign up, ignore this email.")
    return send(to, "Confirm your GK Mentor email", t, h)

def reset(to, link):
    t, h = _wrap("Reset your password", "Someone (hopefully you) asked to reset the password for this GK Mentor account.",
                 link, "Choose a new password", "The link works for 1 hour and only once. If you did not ask for this, ignore this email: your password stays the same.")
    return send(to, "Reset your GK Mentor password", t, h)
