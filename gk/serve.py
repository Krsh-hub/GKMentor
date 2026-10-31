"""Public site server: serves site/ and handles accounts. Run: python run.py serve   (for real hosting put it behind HTTPS)."""
import json, mimetypes, os, sys, threading, time, traceback
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from . import db, users, config, admin, site, jobs

_MAX_POST_BODY = 20_000                     # 20 KB cap on POST body to prevent abuse
_MAX_ADMIN_BODY = 500_000                   # 500 KB cap on admin POST body (e.g. text/drafts/imports)
# per-IP limits: (requests, seconds). Stops email bombing via "forgot password" and guessing the admin password.
_LIMITS = {"mail": (6, 900), "signup": (10, 3600), "login": (30, 900), "admin_fail": (10, 900)}

class _Limiter:
    def __init__(self): self.hits, self.lock = defaultdict(deque), threading.Lock()
    def _q(self, kind, key):
        q, win = self.hits[(kind, key)], _LIMITS[kind][1]
        while q and q[0] < time.time() - win: q.popleft()
        return q
    def blocked(self, kind, key):
        with self.lock: return len(self._q(kind, key)) >= _LIMITS[kind][0]
    def hit(self, kind, key):
        with self.lock: self._q(kind, key).append(time.time())
    def allow(self, kind, key):
        """Count this request; False if over the limit."""
        with self.lock:
            q = self._q(kind, key)
            if len(q) >= _LIMITS[kind][0]: return False
            q.append(time.time()); return True

def make_public_server(port=5080, host="127.0.0.1", db_path=None, site_dir=None, verifier=users.google_verifier):
    site_dir = Path(site_dir or config.ROOT / "site").resolve()
    client_id = os.getenv("GK_GOOGLE_CLIENT_ID", "")
    secure = os.getenv("GK_SECURE_COOKIE") == "1"
    valid = set(config.load_lenses())
    allowed_origin_env = os.getenv("GK_ALLOWED_ORIGIN") or os.getenv("GK_ALLOWED_ORIGINS") or ""
    allowed_netlocs = {urlparse(x.strip()).netloc for x in allowed_origin_env.split(",") if x.strip()}
    public_url = (os.getenv("GK_PUBLIC_URL") or "").rstrip("/")
    limiter = _Limiter()
    ctx = {"db_path": db_path, "site_dir": site_dir}
    _c = db.connect(db_path); jobs.recover(_c); _c.close()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, obj=None, ctype="application/json", cookie=None, raw=None, extra=None):
            raw = raw if raw is not None else json.dumps(obj).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
            self.send_header("Content-Length", str(len(raw))); self.send_header("Cache-Control", "no-store" if ctype == "application/json" else "no-cache")
            # Security headers
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            o = self.headers.get("Origin")
            if o and (urlparse(o).netloc == self.headers.get("Host") or urlparse(o).netloc in allowed_netlocs):
                self.send_header("Access-Control-Allow-Origin", o)
                self.send_header("Access-Control-Allow-Credentials", "true")
            if secure:
                self.send_header("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
            if cookie: self.send_header("Set-Cookie", cookie)
            for k, v in (extra or {}).items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(raw)
        def _ip(self):
            # behind Vercel/Render the client is the first X-Forwarded-For entry
            return (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0]
        def _base(self):
            """Where links in emails point. Set GK_PUBLIC_URL (e.g. https://your-site.vercel.app) in production."""
            if public_url: return public_url
            return f"{'https' if secure else 'http'}://{self.headers.get('Host') or 'localhost'}"
        def _admin_ok(self):
            if limiter.blocked("admin_fail", self._ip()): return False
            ok = admin.is_admin_authorized(self.headers, self.client_address[0])
            if not ok and (self.headers.get("X-Admin-Key") or self.headers.get("Authorization")): limiter.hit("admin_fail", self._ip())
            return ok
        def _tok(self):
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c["gk_session"].value if "gk_session" in c else None
        def _set(self, tok, clear=False):
            samesite = "None" if (secure and allowed_netlocs) else "Lax"
            sec_flag = "; Secure" if (secure or samesite == "None") else ""
            return f"gk_session={'' if clear else tok}; Path=/; HttpOnly; SameSite={samesite}; Max-Age={0 if clear else users.SESSION_DAYS * 86400}{sec_flag}"
        def _origin_ok(self):
            o = self.headers.get("Origin")
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return False
            if not o:
                return True
            onet = urlparse(o).netloc
            return onet == self.headers.get("Host") or onet in allowed_netlocs
        def do_OPTIONS(self):
            o = self.headers.get("Origin")
            self.send_response(204)
            if o and (urlparse(o).netloc == self.headers.get("Host") or urlparse(o).netloc in allowed_netlocs):
                self.send_header("Access-Control-Allow-Origin", o)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Cookie, X-Admin-Key, Authorization")
            self.end_headers()
        def do_GET(self):
            u = urlparse(self.path); con = db.connect(db_path)
            try:
                if u.path == "/auth/config": return self._send(200, {"googleClientId": client_id})
                if u.path == "/auth/me": return self._send(200, {"user": users.public(users.user_for(con, self._tok()))})
                if u.path == "/auth/verify":                     # the link in the confirmation email
                    ok = users.verify_email(con, (parse_qs(u.query).get("token") or [""])[0])
                    return self._send(302, {}, extra={"Location": f"{public_url}/?verified={'1' if ok else '0'}"})
                if u.path == "/api/content":
                    return self._send(200, site.cached_site_data(con))
                if u.path in ("/admin", "/admin/"):
                    return self._send(200, raw=admin._page().encode("utf-8"), ctype="text/html")
                if u.path.startswith(("/api/admin/", "/api/static/")) or u.path in ("/api/meta", "/api/notes", "/api/gd", "/api/editorials"):
                    if not self._admin_ok():
                        return self._send(401, {"error": "password required"})
                    code, resp = admin.handle_get(con, u.path, u.query, ctx)
                    return self._send(code, resp)
                if u.path in ("/api/state", "/auth/export"):
                    me = users.user_for(con, self._tok())
                    if not me: return self._send(401, {"error": "Log in first."})
                    return self._send(200, users.get_state(con, me["id"]) if u.path == "/api/state" else users.export(con, me["id"]))
                f = (site_dir / (u.path.lstrip("/") or "index.html")).resolve()
                if site_dir in f.parents and f.is_file() and f.suffix in (".html", ".js", ".css", ".json", ".png", ".svg", ".ico"):
                    return self._send(200, ctype=mimetypes.guess_type(f.name)[0] or "application/octet-stream", raw=f.read_bytes())
                self._send(404, {"error": "not found"})
            finally: con.close()
        def do_POST(self):
            u = urlparse(self.path)
            if not self._origin_ok(): return self._send(403, {"error": "Bad request origin."})
            con = db.connect(db_path)
            try:
                is_admin_call = (u.path.startswith(("/api/admin/", "/api/static/", "/api/gd/", "/api/editorial/"))
                                 or u.path in ("/api/note", "/api/question", "/api/editorial", "/api/gd", "/api/build"))
                body_limit = _MAX_ADMIN_BODY if is_admin_call else _MAX_POST_BODY
                d = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), body_limit)) or b"{}")

                if is_admin_call:
                    if not self._admin_ok():
                        return self._send(401, {"error": "password required"})
                    if u.path == "/api/build":
                        _, n, q, e = site.build_site(con, site_dir)
                        return self._send(200, {"notes": n, "questions": q, "editorials": e})
                    code, resp = admin.handle_post(con, u.path, d, ctx=ctx)
                    return self._send(code, resp)

                ip = self._ip()
                if u.path in ("/auth/signup", "/auth/login", "/auth/google", "/auth/reset"):
                    if not limiter.allow("signup" if u.path == "/auth/signup" else "login", ip):
                        return self._send(429, {"error": "Too many attempts from your network. Try again later."})
                    if u.path == "/auth/signup":
                        r = users.signup(con, d, valid)
                        if "id" in r and limiter.allow("mail", ip): users.send_verification(con, r["id"], self._base())
                    elif u.path == "/auth/login": r = users.login(con, d.get("email"), d.get("password"))
                    elif u.path == "/auth/reset": r = users.reset_password(con, d.get("token"), d.get("password"))
                    else: r = users.google_login(con, d.get("credential"), client_id, verifier)
                    if "error" in r: return self._send(400, r)
                    return self._send(200, {"user": users.public(con.execute("SELECT * FROM users WHERE id=?", (r["id"],)).fetchone())},
                                      cookie=self._set(users.new_session(con, r["id"])))
                if u.path == "/auth/forgot":
                    if not limiter.allow("mail", ip): return self._send(429, {"error": "Too many emails requested. Try again in a few minutes."})
                    return self._send(200, users.request_reset(con, d.get("email"), self._base()))
                if u.path == "/auth/logout":
                    users.end_session(con, self._tok()); return self._send(200, {"ok": True}, cookie=self._set("", clear=True))
                me = users.user_for(con, self._tok())
                if not me: return self._send(401, {"error": "Log in first."})
                if u.path == "/auth/profile":
                    r = users.complete_profile(con, me["id"], d, valid)
                    return self._send(400 if "error" in r else 200, r if "error" in r else {"user": users.public(con.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone())})
                if u.path == "/api/state": users.set_state(con, me["id"], d); return self._send(200, {"ok": True})
                if u.path == "/auth/resend-verification":
                    if not limiter.allow("mail", ip): return self._send(429, {"error": "Too many emails requested. Try again in a few minutes."})
                    r = users.send_verification(con, me["id"], self._base())
                    return self._send(400 if "error" in r else 200, r)
                if u.path == "/auth/password":
                    r = users.change_password(con, me["id"], d.get("old_password"), d.get("new_password"))
                    if "error" in r: return self._send(400, r)
                    return self._send(200, {"ok": True}, cookie=self._set(users.new_session(con, me["id"])))
                if u.path == "/auth/delete" and d.get("confirm"):
                    users.delete(con, me["id"]); return self._send(200, {"ok": True}, cookie=self._set("", clear=True))
                self._send(404, {"error": "not found"})
            except Exception as ex:
                print(f"[serve] ERROR {u.path}: {ex}", file=sys.stderr, flush=True)
                traceback.print_exc(file=sys.stderr)
                self._send(500, {"error": "Something went wrong."})
            finally: con.close()
    return ThreadingHTTPServer((host, port), H)

def serve(port=5080, host="127.0.0.1"):
    srv = make_public_server(port, host)
    secure = os.getenv("GK_SECURE_COOKIE") == "1"
    from . import mailer
    if not os.getenv("GK_ADMIN_PASSWORD"): print("NOTE: GK_ADMIN_PASSWORD is not set, so /admin only works from this machine.", file=sys.stderr)
    if not os.getenv("GK_PUBLIC_URL"): print("NOTE: set GK_PUBLIC_URL (your site's https address) so links in emails point to it.", file=sys.stderr)
    print(f"Email sending: {mailer.provider()}" + ("  (emails are only printed here: set RESEND_API_KEY or GK_SMTP_HOST)" if mailer.provider() == "log" else ""))
    print(f"GK site running at http://localhost:{port}/  (Ctrl+C to stop)")
    if not os.getenv("GK_GOOGLE_CLIENT_ID"): print("Google sign-in is OFF (set GK_GOOGLE_CLIENT_ID to turn it on).")
    if not secure: print("WARNING: Cookies are NOT marked Secure. Set GK_SECURE_COOKIE=1 and serve behind HTTPS before going public.", file=sys.stderr)
    try: srv.serve_forever()
    except KeyboardInterrupt: print("stopped")

