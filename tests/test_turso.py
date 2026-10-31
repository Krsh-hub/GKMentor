"""The Turso adapter, end to end against a fake Turso server (Hrana over HTTP v2, backed by a real SQLite file)."""
import base64, json, sqlite3, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from test_pipeline import DEMO, fresh, _pub, _r, GOOD
from gk import db, turso, pipeline, site, users, static

def _enc(v):
    if v is None: return {"type": "null"}
    if isinstance(v, int): return {"type": "integer", "value": str(v)}
    if isinstance(v, float): return {"type": "float", "value": v}
    if isinstance(v, bytes): return {"type": "blob", "base64": base64.b64encode(v).decode()}
    return {"type": "text", "value": v}

def _dec(a):
    t = a["type"]
    return None if t == "null" else int(a["value"]) if t == "integer" else a["value"] if t in ("float", "text") else base64.b64decode(a["base64"] + "==")

class FakeTurso:
    """Minimal Hrana v2 /v2/pipeline server: execute (positional/named args), sequence, close."""
    def __init__(self, path, token="tok"):
        self.sql = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock, self.requests, me = threading.Lock(), 0, self
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"                       # keep-alive, like Turso
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/v2/pipeline" or self.headers.get("Authorization") != f"Bearer {token}":
                    out, code = b'{"error":"unauthorized"}', 401
                else:
                    out, code = json.dumps({"baton": None, "base_url": None, "results": [me.handle(r) for r in body["requests"]]}).encode(), 200
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(out)))
                self.end_headers(); self.wfile.write(out)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def handle(self, r):
        with self.lock:
            self.requests += 1
            try:
                if r["type"] == "close": return {"type": "ok", "response": {"type": "close"}}
                if r["type"] == "sequence":
                    self.sql.executescript(r["sql"]); return {"type": "ok", "response": {"type": "sequence"}}
                st = r["stmt"]
                args = {a["name"].lstrip(":@$"): _dec(a["value"]) for a in st["named_args"]} if st.get("named_args") else [_dec(a) for a in st.get("args", [])]
                cur = self.sql.execute(st["sql"], args)
                rows = cur.fetchall()
                return {"type": "ok", "response": {"type": "execute", "result": {
                    "cols": [{"name": d[0], "decltype": None} for d in (cur.description or [])],
                    "rows": [[_enc(v) for v in row] for row in rows],
                    "affected_row_count": max(cur.rowcount, 0), "last_insert_rowid": str(cur.lastrowid) if cur.lastrowid else None}}}
            except sqlite3.Error as e:
                return {"type": "error", "error": {"message": str(e), "code": "SQLITE_CONSTRAINT" if isinstance(e, sqlite3.IntegrityError) else "SQLITE_ERROR"}}

@pytest.fixture
def remote(tmp_path, monkeypatch):
    f = FakeTurso(str(tmp_path / "remote.db"))
    monkeypatch.setenv("TURSO_DATABASE_URL", f.url); monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    fresh(tmp_path)                                    # chdir + OUT_DIR like the other tests
    yield f
    f.srv.shutdown()

def test_adapter_basics(remote):
    con = db.connect()
    assert type(con).__module__ == "gk.turso" and db.where().startswith("Turso: 127.0.0.1")
    cur = con.execute("INSERT INTO editorials(day,title,body_md) VALUES(?,?,?)", ("2026-10-31", "T", "B"))
    assert cur.lastrowid == 1 and cur.rowcount == 1
    r = con.execute("SELECT id, title, body_md, NULL AS z, 1.5 AS f FROM editorials").fetchone()
    assert r["title"] == "T" and r[0] == 1 and r["z"] is None and r["f"] == 1.5 and dict(r)["body_md"] == "B" and tuple(r)[:2] == (1, "T")
    assert [x["id"] for x in con.execute("SELECT id FROM editorials")] == [1]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO users(email) VALUES('a@x.com')"); con.execute("INSERT INTO users(email) VALUES('a@x.com')")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("SELECT * FROM no_such_table")
    assert con.execute("SELECT :a + :b AS s", {"a": 2, "b": 3}).fetchone()["s"] == 5
    assert con.execute("SELECT ? AS b", (b"\x00\x01",)).fetchone()["b"] == b"\x00\x01"
    n = remote.requests; db.connect(); db.connect()
    assert remote.requests - n < 4                                     # schema setup is not repeated on every connect

def test_bad_token_is_a_clear_error(remote, monkeypatch):
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "wrong")
    with pytest.raises(sqlite3.OperationalError, match="401"):
        turso.connect().execute("SELECT 1")

def test_pipeline_site_and_accounts_run_on_turso(remote, tmp_path):
    con = db.connect()
    s = pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=con)
    assert s["notes"] == 1
    con.execute("UPDATE notes SET status='approved'"); con.execute("UPDATE questions SET status='approved'")
    static.add_item(con, "countries", {"country": "Japan", "capital": "Tokyo", "currency": "Yen", "continent": "Asia"}, "approved")
    data = site.cached_site_data(con)
    assert len(data["notes"]) == 1 and data["notes"][0]["facts"] and data["questions"]
    assert site.cached_site_data(con) is data                          # cached until the next write...
    con.execute("UPDATE notes SET headline='Changed'")
    assert site.cached_site_data(con)["notes"][0]["headline"] == "Changed"   # ...which invalidates it
    users._fails.clear()
    assert "id" in users.signup(con, GOOD) and "id" in users.login(con, "asha@example.com", "correct horse")

def test_public_server_on_turso(remote, tmp_path):
    from gk.serve import make_public_server
    (tmp_path / "site").mkdir(exist_ok=True)
    srv = make_public_server(0, db_path=None, site_dir=tmp_path / "site")
    threading.Thread(target=srv.serve_forever, daemon=True).start(); port = srv.server_address[1]
    try:
        st, d, ck, _ = _r(port, "POST", "/auth/signup", GOOD)
        assert st == 200 and _r(port, "GET", "/auth/me", cookie=ck)[1]["user"]["email"] == "asha@example.com"
        assert _r(port, "GET", "/api/content")[0] == 200
    finally:
        srv.shutdown()
    assert sqlite3.connect(str(tmp_path / "remote.db")).execute("SELECT email FROM users").fetchone()[0] == "asha@example.com"   # it really is in "Turso"

def test_copy_local_database_to_turso(remote, tmp_path):
    local = str(tmp_path / "local.db")
    lc = db.connect(local); pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=lc)
    users.signup(lc, GOOD); lc.close()
    rep = db.copy_to_turso(local)
    assert all(a == b for a, b in rep.values()) and rep["notes"][0] == 1 and rep["users"][0] == 1
    with pytest.raises(SystemExit, match="already has data"):
        db.copy_to_turso(local)
    assert db.copy_to_turso(local, replace=True)["notes"] == (1, 1)    # --replace wipes and copies again, no duplicates
    con = db.connect()
    assert con.execute("SELECT pw_hash FROM users").fetchone()[0].startswith("scrypt$")
