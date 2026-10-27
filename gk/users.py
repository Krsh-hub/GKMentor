"""User accounts: email+password (scrypt-hashed) and Google sign-in. Standard library only (Google token check uses google-auth)."""
import hashlib, hmac, json, re, secrets, time
from datetime import datetime, timezone

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

POLICY = "2026-10-v1"                       # bump when the privacy policy changes; stored with each consent
AGE_BANDS = ("Under 18", "18-24", "25+")
SESSION_DAYS = 30
_fails = {}                                  # email -> (failed attempts, locked-until); in memory, resets on restart

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
                                   plan=u["plan"], profile_complete=bool(u["consent_version"] and u["age_band"]))

def _clean_exams(exams, valid):
    return ",".join(x for x in (exams or []) if x in valid)

def _consent(con, uid, age, exams, valid):
    con.execute("UPDATE users SET age_band=?, exams=?, consent_version=?, consent_at=? WHERE id=?",
                (age, _clean_exams(exams, valid), POLICY, _now(), uid))

def signup(con, d, valid_exams=()):
    email, pw, name = (d.get("email") or "").strip().lower(), d.get("password") or "", (d.get("name") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email): return {"error": "Enter a valid email address."}
    if not (8 <= len(pw) <= 128): return {"error": "Password must be 8 to 128 characters."}
    if not (1 <= len(name) <= 60): return {"error": "Enter a name or nickname (up to 60 characters)."}
    if d.get("age_band") not in AGE_BANDS: return {"error": "Choose your age band."}
    if not d.get("consent"): return {"error": "Please accept the privacy policy to continue."}
    if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(): return {"error": "This email is already registered. Try logging in."}
    cur = con.execute("INSERT INTO users(email,name,pw_hash) VALUES(?,?,?)", (email, name, hash_pw(pw)))
    _consent(con, cur.lastrowid, d["age_band"], d.get("exams"), valid_exams); con.commit()
    return {"id": cur.lastrowid}

def login(con, email, pw):
    email = (email or "").strip().lower()
    n, until = _fails.get(email, (0, 0))
    if until > time.time(): return {"error": "Too many attempts. Try again in a few minutes."}
    u = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not u["pw_hash"] or not check_pw(pw or "", u["pw_hash"]):
        n += 1; _fails[email] = (n, time.time() + 900 if n >= 5 else 0)
        return {"error": "Wrong email or password."}                 # same message whether or not the email exists
    _fails.pop(email, None)
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
    if u:
        con.execute("UPDATE users SET google_sub=?, last_login=? WHERE id=?", (info["sub"], _now(), u["id"])); uid = u["id"]
    else:
        uid = con.execute("INSERT INTO users(email,name,google_sub) VALUES(?,?,?)", (email, (info.get("name") or email.split("@")[0])[:60], info["sub"])).lastrowid
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
    for t, c in (("sessions", "user_id"), ("user_state", "user_id"), ("users", "id")):
        con.execute(f"DELETE FROM {t} WHERE {c}=?", (uid,))
    con.commit()
