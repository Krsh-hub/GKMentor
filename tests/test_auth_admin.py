"""Email verification, password reset, Google account linking, rate limits, and the admin panel's background jobs."""
import json, re, time
from test_pipeline import _pub, _r, GOOD, fresh, _published_demo
from gk import users, mailer, jobs, admin
from gk.llm import MockLLM

def _link(kind):
    m = re.search(r"(/auth/verify\?token=|/\?reset=)([\w-]+)", [x for x in mailer.OUTBOX if kind in x["subject"]][-1]["text"])
    return m.group(2)

def test_verify_email_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv("GK_PUBLIC_URL", "https://gk.example")
    mailer.OUTBOX.clear(); srv, port = _pub(tmp_path)
    try:
        st, d, ck, _ = _r(port, "POST", "/auth/signup", GOOD)
        assert st == 200 and d["user"]["email_verified"] is False and d["user"]["has_password"] is True
        msg = mailer.OUTBOX[-1]
        assert msg["to"] == "asha@example.com" and "https://gk.example/auth/verify?token=" in msg["text"]
        tok = _link("Confirm")
        assert _r(port, "POST", "/auth/resend-verification", {}, cookie=ck)[0] == 400          # under a minute: no second email
        st, _, _, _ = _r(port, "GET", "/auth/verify?token=nope")
        assert st == 302
        c = __import__("http.client").client.HTTPConnection("127.0.0.1", port); c.request("GET", f"/auth/verify?token={tok}")
        r = c.getresponse(); assert r.status == 302 and r.getheader("Location") == "https://gk.example/?verified=1"
        assert _r(port, "GET", "/auth/me", cookie=ck)[1]["user"]["email_verified"] is True
        c.request("GET", f"/auth/verify?token={tok}"); r = c.getresponse()                     # single use
        assert r.getheader("Location").endswith("verified=0")
    finally:
        srv.shutdown()

def test_password_reset_flow(tmp_path):
    mailer.OUTBOX.clear(); users._fails.clear(); srv, port = _pub(tmp_path)
    try:
        st, d, ck, _ = _r(port, "POST", "/auth/signup", GOOD)
        n = len(mailer.OUTBOX)
        assert _r(port, "POST", "/auth/forgot", {"email": "ghost@example.com"})[1] == {"ok": True}   # same answer for unknown email
        assert len(mailer.OUTBOX) == n                                                             # ...but nothing sent
        assert _r(port, "POST", "/auth/forgot", {"email": " ASHA@example.com "})[1] == {"ok": True}
        tok = _link("Reset")
        assert _r(port, "POST", "/auth/reset", {"token": tok, "password": "short"})[0] == 400
        assert _r(port, "POST", "/auth/reset", {"token": "wrong", "password": "brand new pw"})[0] == 400
        st, d, ck2, _ = _r(port, "POST", "/auth/reset", {"token": tok, "password": "brand new pw"})
        assert st == 200 and ck2 and d["user"]["email_verified"] is True                         # inbox proven by the link
        assert _r(port, "GET", "/auth/me", cookie=ck)[1]["user"] is None                         # old sessions signed out
        assert _r(port, "POST", "/auth/reset", {"token": tok, "password": "again again"})[0] == 400   # single use
        assert _r(port, "POST", "/auth/login", dict(email="asha@example.com", password="correct horse"))[0] == 400
        st, _, ck3, _ = _r(port, "POST", "/auth/login", dict(email="asha@example.com", password="brand new pw"))
        assert st == 200
        assert _r(port, "POST", "/auth/password", dict(old_password="wrong", new_password="third password"), cookie=ck3)[0] == 400
        st, _, ck4, _ = _r(port, "POST", "/auth/password", dict(old_password="brand new pw", new_password="third password"), cookie=ck3)
        assert st == 200 and _r(port, "GET", "/auth/me", cookie=ck4)[1]["user"] and _r(port, "GET", "/auth/me", cookie=ck3)[1]["user"] is None
    finally:
        srv.shutdown()

def test_forgot_password_is_rate_limited_per_ip(tmp_path):
    srv, port = _pub(tmp_path)
    try:
        codes = [_r(port, "POST", "/auth/forgot", {"email": f"x{i}@example.com"})[0] for i in range(8)]
        assert codes[:6] == [200] * 6 and codes[-1] == 429
    finally:
        srv.shutdown()

def test_google_takes_over_unverified_squatter_account(tmp_path, monkeypatch):
    """Someone signs up with the victim's email and a password; the real owner later uses Google -> the squatter loses access."""
    monkeypatch.setenv("GK_GOOGLE_CLIENT_ID", "cid")
    ver = lambda cred, cid: dict(sub="g-owner", email="asha@example.com", email_verified=True, name="Asha")
    srv, port = _pub(tmp_path, verifier=ver); users._fails.clear()
    try:
        _, _, squat, _ = _r(port, "POST", "/auth/signup", GOOD)
        st, d, ck, _ = _r(port, "POST", "/auth/google", {"credential": "ok"})
        assert st == 200 and d["user"]["email_verified"] and not d["user"]["has_password"]
        assert _r(port, "GET", "/auth/me", cookie=squat)[1]["user"] is None
        assert _r(port, "POST", "/auth/login", dict(email="asha@example.com", password="correct horse"))[0] == 400
    finally:
        srv.shutdown()

def test_verified_password_account_keeps_password_when_linking_google(tmp_path):
    con = fresh(tmp_path); uid = users.signup(con, GOOD)["id"]
    con.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,)); con.commit()
    r = users.google_login(con, "x", "cid", lambda c, i: dict(sub="g1", email="asha@example.com", email_verified=True))
    assert r["id"] == uid and con.execute("SELECT pw_hash FROM users").fetchone()[0]
    assert "different Google" in users.google_login(con, "x", "cid", lambda c, i: dict(sub="g2", email="asha@example.com", email_verified=True))["error"]

def test_admin_auth_ignores_forged_host_header(monkeypatch):
    monkeypatch.delenv("GK_ADMIN_PASSWORD", raising=False)
    assert admin.is_admin_authorized({"Host": "localhost:5080"}, "127.0.0.1")
    assert not admin.is_admin_authorized({"Host": "localhost"}, "203.0.113.9")                    # remote client pretending to be local
    assert not admin.is_admin_authorized({"Host": "localhost", "X-Forwarded-For": "203.0.113.9"}, "127.0.0.1")   # via a proxy
    monkeypatch.setenv("GK_ADMIN_PASSWORD", "pw")
    assert admin.is_admin_authorized({"X-Admin-Key": "pw"}) and admin.is_admin_authorized({"Authorization": "Bearer pw"})
    assert not admin.is_admin_authorized({"X-Admin-Key": "nope"}) and not admin.is_admin_authorized({})

def _wait(port, jid, key):
    for _ in range(100):
        j = _r(port, "GET", f"/api/admin/jobs/{jid}", headers={"X-Admin-Key": key})[1]
        if j["status"] != "running": return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")

def test_admin_jobs_run_in_background_on_public_server(tmp_path, monkeypatch):
    monkeypatch.setenv("GK_ADMIN_PASSWORD", "pw"); monkeypatch.setenv("GK_PROVIDER", "mock")
    srv, port = _pub(tmp_path); H = {"X-Admin-Key": "pw"}
    try:
        assert _r(port, "GET", "/api/admin/jobs")[0] == 401
        ov = _r(port, "GET", "/api/admin/jobs", headers=H)[1]
        assert {k["kind"] for k in ov["kinds"]} >= {"everyday", "daily", "gd", "build", "publish"} and ov["running"] is None
        assert _r(port, "POST", "/api/admin/jobs", {"kind": "nope"}, headers=H)[0] == 409
        assert _r(port, "POST", "/api/admin/jobs", {"kind": "build", "params": {"day": "31-10-2026"}}, headers=H)[0] == 400
        st, d, _, _ = _r(port, "POST", "/api/admin/jobs", {"kind": "build"}, headers=H)
        assert st == 202
        j = _wait(port, d["id"], "pw")
        assert j["status"] == "done" and "started" in j["log"] and j["result"]["notes"] == 0
        assert (tmp_path / "site" / "data.js").exists()
        st, d, _, _ = _r(port, "POST", "/api/admin/jobs", {"kind": "publish"}, headers=H)          # not configured -> job fails cleanly
        assert "not set up" in _wait(port, d["id"], "pw")["result"]["error"]
        assert [x["kind"] for x in _r(port, "GET", "/api/admin/jobs", headers=H)[1]["recent"]] == ["publish", "build"]
        # the AI buttons were unreachable on the hosted server before (404): now they are admin calls
        assert _r(port, "POST", "/api/gd/generate", {})[0] == 401
        assert _r(port, "POST", "/api/editorial/draft", {"topic": ""}, headers=H)[0] == 400
    finally:
        srv.shutdown()

def test_only_one_job_at_a_time_and_logs_capture_output(tmp_path, monkeypatch):
    import threading
    gate = threading.Event()
    def slow(con, ctx, p): print("working on it"); gate.wait(5); return {"ok": 1}
    monkeypatch.setitem(jobs.KINDS, "slow", ("Slow", "test", slow))
    fresh(tmp_path); ctx = {"db_path": str(tmp_path / "t.db"), "site_dir": tmp_path / "site"}
    jid, err = jobs.start("slow", {}, ctx)
    assert jid and not err
    assert "still running" in jobs.start("slow", {}, ctx)[1]
    time.sleep(0.2); con = fresh(tmp_path)
    assert "working on it" in jobs.get(con, jid)["log"]
    gate.set()
    for _ in range(50):
        if jobs.get(con, jid)["status"] != "running": break
        time.sleep(0.1)
    assert jobs.get(con, jid)["status"] == "done" and jobs.running() is None

def test_gd_job_with_mock_llm(tmp_path):
    con = _published_demo(tmp_path)
    ctx = {"db_path": str(tmp_path / "t.db"), "site_dir": tmp_path / "site", "llm_factory": lambda: MockLLM()}
    jid, _ = jobs.start("gd", {"day": "2026-10-05"}, ctx)
    for _ in range(50):
        j = jobs.get(con, jid)
        if j["status"] != "running": break
        time.sleep(0.1)
    assert j["status"] == "done" and j["result"]["gd_topics_drafted"] == 1

def test_reseed_from_data_js_keeps_questions_and_static(tmp_path, monkeypatch):
    from gk import site, db, config
    con = _published_demo(tmp_path)
    data = site.build_site_data(con)
    assert data["questions"] and all(q["note_id"] for q in data["questions"])
    import shutil
    root = tmp_path / "root"; (root / "site").mkdir(parents=True); shutil.copytree(config.ROOT / "config", root / "config")
    (root / "site" / "data.js").write_text("window.GK_DATA=" + json.dumps(data) + ";", encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", root)
    c2 = db.connect(str(tmp_path / "fresh.db")); db.seed_if_empty(c2)
    again = site.build_site_data(c2)
    assert len(again["questions"]) == len(data["questions"]) and len(again["notes"]) == len(data["notes"])
    assert sum(len(c["items"]) for c in again["static"]) == sum(len(c["items"]) for c in data["static"])
