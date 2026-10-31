"""User accounts: email+password (scrypt-hashed) and Google sign-in. Standard library only (Google token check uses google-auth).
Email verification and password reset use single-use tokens; only their SHA-256 hash is stored."""
import hashlib, hmac, json, re, secrets, time
from . import mailer
from datetime import datetime, timezone

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

POLICY = "2026-10-v1"                       # bump when the privacy policy changes; stored with each consent
AGE_BANDS = ("Under 18", "18-24", "25+")
SESSION_DAYS = 30
_LOCKOUT_SECS = 900                          # 15-minute lockout after too many failed logins
_MAX_FAILS = 5                               # lock after this many failures
VERIFY_SECS = 48 * 3600                      # email-confirmation link lifetime
RESET_SECS = 3600                            # password-reset link lifetime
RESEND_GAP = 60                              # at most one email of a kind per account per minute

class _FailsTracker(dict):
    def clear(self):
        super().clear()
        if _last_con:
            try:
                _last_con.execute("DELETE FROM login_attempts")
                _last_con.commit()
            except Exception:
                pass

_fails = _FailsTracker()
_last_con = None

def _scrypt(pw, salt):
    return hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)

def hash_pw(pw):
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${_scrypt(pw, salt).hex()}"

def check_pw(pw, stored):
    try:
        _, s, h = stored.split("$")
        return hmac.compare_digest(_scrypt(pw, bytes.fromhex(s)).hex(), h)
    except Exception:
        return False

def public(u):
    return None if not u else dict(id=u["id"], email=u["email"], name=u["name"], age_band=u["age_band"], exams=[x for x in (u["exams"] or "").split(",") if x],
                                   plan=u["plan"], profile_complete=bool(u["consent_version"] and u["age_band"]),
                                   email_verified=bool(_get(u, "email_verified")), has_password=bool(u["pw_hash"]))

def _get(row, k, default=None):
    try: return row[k]
    except (IndexError, KeyError): return default

def _clean_exams(exams, valid):
    return ",".join(x for x in (exams or []) if x in valid)

def _consent(con, uid, age, exams, valid):
    con.execute("UPDATE users SET age_band=?, exams=?, consent_version=?, consent_at=? WHERE id=?",
                (age, _clean_exams(exams, valid), POLICY, _now(), uid))

def _check_lockout(con, email):
    """Return (fails, locked_until) from DB or memory. Survives server restarts."""
    global _last_con
    _last_con = con
    try:
        r = con.execute("SELECT fails, locked_until FROM login_attempts WHERE email=?", (email,)).fetchone()
        if r:
            _fails[email] = (r["fails"], r["locked_until"])
            return (r["fails"], r["locked_until"])
    except Exception:
        pass
    return _fails.get(email, (0, 0))

def _record_fail(con, email):
    """Increment the fail counter; lock the account after _MAX_FAILS failures."""
    global _last_con
    _last_con = con
    n = 0
    try:
        r = con.execute("SELECT fails FROM login_attempts WHERE email=?", (email,)).fetchone()
        n = (r["fails"] if r else 0)
    except Exception:
        n = _fails.get(email, (0, 0))[0]
    n += 1
    lock = time.time() + _LOCKOUT_SECS if n >= _MAX_FAILS else 0
    _fails[email] = (n, lock)
    try:
        con.execute("INSERT OR REPLACE INTO login_attempts(email, fails, locked_until) VALUES(?,?,?)",
                    (email, n, lock))
        con.commit()
    except Exception:
        pass

def _clear_fails(con, email):
    global _last_con
    _last_con = con
    _fails.pop(email, None)
    try:
        con.execute("DELETE FROM login_attempts WHERE email=?", (email,))
        con.commit()
    except Exception:
        pass

def signup(con, d, valid_exams=()):
    email, pw, name = (d.get("email") or "").strip().lower(), d.get("password") or "", (d.get("name") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email): return {"error": "Enter a valid email address."}
    if not (8 <= len(pw) <= 128): return {"error": "Password must be 8 to 128 characters."}
    if not (1 <= len(name) <= 60): return {"error": "Enter a name or nickname (up to 60 characters)."}
    if d.get("age_band") not in AGE_BANDS: return {"error": "Choose your age band."}
    if not d.get("consent"): return {"error": "Please accept the privacy policy to continue."}
    if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(): return {"error": "This email is already registered. Try logging in, or use 'Forgot password'."}
    cur = con.execute("INSERT INTO users(email,name,pw_hash) VALUES(?,?,?)", (email, name, hash_pw(pw)))
    _consent(con, cur.lastrowid, d["age_band"], d.get("exams"), valid_exams); con.commit()
    return {"id": cur.lastrowid}

def login(con, email, pw):
    email = (email or "").strip().lower()
    n, until = _check_lockout(con, email)
    if until > time.time(): return {"error": "Too many attempts. Try again in a few minutes."}
    u = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not u["pw_hash"] or not check_pw(pw or "", u["pw_hash"]):
        _record_fail(con, email)
        return {"error": "Wrong email or password."}                 # same message whether or not the email exists
    _clear_fails(con, email)
    con.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), u["id"])); con.commit()
    return {"id": u["id"]}

def google_verifier(credential, client_id):
    from google.oauth2 import id_token
    from google.auth.transport import requests as greq
    return id_token.verify_oauth2_token(credential, greq.Request(), client_id)

def google_login(con, credential, client_id, verifier=google_verifier):
    if not client_id: return {"error": "Google sign-in is not set up on this site."}
    try: info = verifier(credential, client_id)
    except Exception: return {"error": "Google sign-in failed. Please try again."}
    email = (info.get("email") or "").lower()
    if not email or not info.get("email_verified"): return {"error": "Your Google email is not verified."}
    u = con.execute("SELECT * FROM users WHERE google_sub=?", (info["sub"],)).fetchone() or con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u and u["google_sub"] and u["google_sub"] != info["sub"]:
        return {"error": "This email is linked to a different Google account."}
    if u:
        if not u["google_sub"] and u["pw_hash"] and not _get(u, "email_verified"):
            # Someone signed up with this email but never proved they own it. Google just proved the real owner is here:
            # drop that unverified password and its sessions so a squatter cannot keep access to the owner's account.
            con.execute("UPDATE users SET pw_hash=NULL WHERE id=?", (u["id"],))
            con.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
        con.execute("UPDATE users SET google_sub=?, email_verified=1, last_login=? WHERE id=?", (info["sub"], _now(), u["id"])); uid = u["id"]
    else:
        uid = con.execute("INSERT INTO users(email,name,google_sub,email_verified) VALUES(?,?,?,1)", (email, (info.get("name") or email.split("@")[0])[:60], info["sub"])).lastrowid
    con.commit()
    return {"id": uid}

def complete_profile(con, uid, d, valid_exams=()):
    if d.get("age_band") not in AGE_BANDS: return {"error": "Choose your age band."}
    if not d.get("consent"): return {"error": "Please accept the privacy policy to continue."}
    _consent(con, uid, d["age_band"], d.get("exams"), valid_exams); con.commit()
    return {"ok": True}

def new_session(con, uid):
    tok = secrets.token_urlsafe(32)
    con.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), uid, time.time() + SESSION_DAYS * 86400)); con.commit()
    return tok

def user_for(con, tok):
    if not tok: return None
    h = hashlib.sha256(tok.encode()).hexdigest()
    r = con.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?", (h, time.time())).fetchone()
    return r

def end_session(con, tok):
    if tok: con.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),)); con.commit()

def get_state(con, uid):
    return {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT key,value FROM user_state WHERE user_id=?", (uid,))}

def set_state(con, uid, d):
    b, h = d.get("bookmarks"), d.get("hist")
    if isinstance(b, list) and len(b) <= 3000 and all(isinstance(x, int) for x in b):
        con.execute("INSERT OR REPLACE INTO user_state VALUES(?,?,?)", (uid, "bookmarks", json.dumps(b)))
    if isinstance(h, list) and len(h) <= 20 and all(isinstance(x, dict) for x in h) and len(json.dumps(h)) < 5000:
        con.execute("INSERT OR REPLACE INTO user_state VALUES(?,?,?)", (uid, "hist", json.dumps(h)))
    con.commit()

def export(con, uid):
    u = dict(con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    u.pop("pw_hash", None)
    return {"account": u, "saved_state": get_state(con, uid)}

def delete(con, uid):
    # also clean up login_attempts for this user's email
    u = con.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    if u:
        con.execute("DELETE FROM login_attempts WHERE email=?", (u["email"],))
    for t, c in (("sessions", "user_id"), ("user_state", "user_id"), ("auth_tokens", "user_id"), ("users", "id")):
        con.execute(f"DELETE FROM {t} WHERE {c}=?", (uid,))
    con.commit()


# ---------- email verification, password reset, password change ----------

def _h(tok):
    return hashlib.sha256((tok or "").encode()).hexdigest()

def _issue(con, uid, kind, ttl):
    """New single-use token of this kind (older ones of the same kind stop working). None if one was sent under a minute ago."""
    last = con.execute("SELECT MAX(created) FROM auth_tokens WHERE user_id=? AND kind=?", (uid, kind)).fetchone()[0]
    if last and time.time() - last < RESEND_GAP: return None
    con.execute("DELETE FROM auth_tokens WHERE user_id=? AND kind=?", (uid, kind))
    tok = secrets.token_urlsafe(32)
    con.execute("INSERT INTO auth_tokens VALUES(?,?,?,?,?)", (_h(tok), uid, kind, time.time() + ttl, time.time())); con.commit()
    return tok

def _take(con, tok, kind):
    """Consume a token: returns its user id, or None if unknown/expired/used."""
    if not tok or len(tok) > 200: return None
    r = con.execute("SELECT user_id FROM auth_tokens WHERE token_hash=? AND kind=? AND expires>?", (_h(tok), kind, time.time())).fetchone()
    if not r: return None
    con.execute("DELETE FROM auth_tokens WHERE user_id=? AND kind=?", (r["user_id"], kind)); con.commit()
    return r["user_id"]

def send_verification(con, uid, base_url):
    u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u: return {"error": "Account not found."}
    if u["email_verified"]: return {"ok": True, "already": True}
    tok = _issue(con, uid, "verify", VERIFY_SECS)
    if not tok: return {"error": "A link was sent less than a minute ago. Check your inbox (and spam folder)."}
    mailer.verification(u["email"], u["name"] or "there", f"{base_url}/auth/verify?token={tok}")
    return {"ok": True}

def verify_email(con, tok):
    uid = _take(con, tok, "verify")
    if uid: con.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,)); con.commit()
    return uid

def request_reset(con, email, base_url):
    """Always answers the same way, so this cannot be used to find out who has an account."""
    email = (email or "").strip().lower()
    u = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone() if email else None
    if u:
        tok = _issue(con, u["id"], "reset", RESET_SECS)
        if tok: mailer.reset(u["email"], f"{base_url}/?reset={tok}")
    return {"ok": True}

def reset_password(con, tok, pw):
    if not (8 <= len(pw or "") <= 128): return {"error": "Password must be 8 to 128 characters."}
    uid = _take(con, tok, "reset")
    if not uid: return {"error": "This reset link is invalid or has expired. Ask for a new one."}
    u = con.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    # the link reached their inbox, so the email is proven; every old session is signed out
    con.execute("UPDATE users SET pw_hash=?, email_verified=1, last_login=? WHERE id=?", (hash_pw(pw), _now(), uid))
    con.execute("DELETE FROM sessions WHERE user_id=?", (uid,)); con.commit()
    _clear_fails(con, u["email"])
    return {"id": uid}

def change_password(con, uid, old, new):
    u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if u["pw_hash"] and not check_pw(old or "", u["pw_hash"]): return {"error": "Your current password is wrong."}
    if not (8 <= len(new or "") <= 128): return {"error": "New password must be 8 to 128 characters."}
    con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(new), uid))
    con.execute("DELETE FROM sessions WHERE user_id=?", (uid,)); con.commit()   # caller issues a fresh session for this device
    return {"id": uid}
