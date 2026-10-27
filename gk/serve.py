"""Public site server: serves site/ and handles accounts. Run: python run.py serve   (for real hosting put it behind HTTPS)."""
import json, mimetypes, os, sys, traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from . import db, users, config

_MAX_POST_BODY = 20_000                     # 20 KB cap on POST body to prevent abuse

def make_public_server(port=5080, host="127.0.0.1", db_path=None, site_dir=None, verifier=users.google_verifier):
    site_dir = Path(site_dir or config.ROOT / "site").resolve()
    client_id = os.getenv("GK_GOOGLE_CLIENT_ID", "")
    secure = os.getenv("GK_SECURE_COOKIE") == "1"
    valid = set(config.load_lenses())
    allowed_origin_env = os.getenv("GK_ALLOWED_ORIGIN") or os.getenv("GK_ALLOWED_ORIGINS") or ""
    allowed_netlocs = {urlparse(x.strip()).netloc for x in allowed_origin_env.split(",") if x.strip()}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, obj=None, ctype="application/json", cookie=None, raw=None):
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
            self.end_headers(); self.wfile.write(raw)
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
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Cookie")
            self.end_headers()
        def do_GET(self):
            u = urlparse(self.path); con = db.connect(db_path)
            try:
                if u.path == "/auth/config": return self._send(200, {"googleClientId": client_id})
                if u.path == "/auth/me": return self._send(200, {"user": users.public(users.user_for(con, self._tok()))})
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
                d = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), _MAX_POST_BODY)) or b"{}")
                if u.path in ("/auth/signup", "/auth/login", "/auth/google"):
                    r = (users.signup(con, d, valid) if u.path == "/auth/signup" else users.login(con, d.get("email"), d.get("password"))
                         if u.path == "/auth/login" else users.google_login(con, d.get("credential"), client_id, verifier))
                    if "error" in r: return self._send(400, r)
                    return self._send(200, {"user": users.public(con.execute("SELECT * FROM users WHERE id=?", (r["id"],)).fetchone())},
                                      cookie=self._set(users.new_session(con, r["id"])))
                if u.path == "/auth/logout":
                    users.end_session(con, self._tok()); return self._send(200, {"ok": True}, cookie=self._set("", clear=True))
                me = users.user_for(con, self._tok())
                if not me: return self._send(401, {"error": "Log in first."})
                if u.path == "/auth/profile":
                    r = users.complete_profile(con, me["id"], d, valid)
                    return self._send(400 if "error" in r else 200, r if "error" in r else {"user": users.public(con.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone())})
                if u.path == "/api/state": users.set_state(con, me["id"], d); return self._send(200, {"ok": True})
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
    print(f"GK site running at http://localhost:{port}/  (Ctrl+C to stop)")
    if not os.getenv("GK_GOOGLE_CLIENT_ID"): print("Google sign-in is OFF (set GK_GOOGLE_CLIENT_ID to turn it on).")
    if not secure: print("WARNING: Cookies are NOT marked Secure. Set GK_SECURE_COOKIE=1 and serve behind HTTPS before going public.", file=sys.stderr)
    try: srv.serve_forever()
    except KeyboardInterrupt: print("stopped")

