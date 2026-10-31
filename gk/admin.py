"""Local admin web app (no Streamlit). Standard library only. Binds to 127.0.0.1.
Set GK_ADMIN_PASSWORD to require a password for every API call."""
import hmac, json, os, re, sys, traceback, webbrowser, mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from . import db, editorial, config
from .site import build_site
from . import usage, static, rates, static_ai, jobs
from .gd import generate as gd_generate, generate_for_topic
from .editorial import draft_from_topic
from .llm import get_llm
from datetime import date

def _page():
    base = Path(__file__).parent
    css = re.search(r"<style>.*?</style>", (base / "site_template.html").read_text(encoding="utf-8"), re.S).group(0)
    return (base / "admin_template.html").read_text(encoding="utf-8").replace("__CSS__", css)

def _notes(con, status, route):
    q = "SELECT * FROM notes WHERE status=?" + ("" if route == "all" else " AND route=?") + " ORDER BY day DESC, id DESC LIMIT 200"
    rows = con.execute(q, (status,) if route == "all" else (status, route)).fetchall()
    ids = [n["id"] for n in rows]; ph = ",".join("?" * len(ids))
    qs, cl = {}, {}
    if ids:                                                   # 2 queries for all notes instead of 2 per note
        for x in con.execute(f"SELECT * FROM questions WHERE note_id IN ({ph}) ORDER BY id", ids):
            m = json.loads(x["body_json"])
            qs.setdefault(x["note_id"], []).append(dict(id=x["id"], q=m["question"], options=m["options"], answer=m["answer_index"],
                                                        valid=x["valid"], solver_ok=x["solver_ok"], status=x["status"]))
        for c in con.execute(f"SELECT * FROM claims WHERE note_id IN ({ph}) ORDER BY id", ids):
            cl.setdefault(c["note_id"], []).append(dict(kind=c["kind"], text=c["text"], grounded=c["grounded"]))
    out = []
    for n in rows:
        b = json.loads(n["body_json"])
        out.append(dict(id=n["id"], day=n["day"], sector=n["sector"], headline=n["headline"], importance=n["importance"],
                        route=n["route"], bullets=b["bullets"], exam_angle=b.get("exam_angle", ""),
                        sources=json.loads(n["source_urls"] or "[]"), claims=cl.get(n["id"], []), questions=qs.get(n["id"], [])))
    return out

def _post_note(con, d):
    n = con.execute("SELECT * FROM notes WHERE id=?", (d["id"],)).fetchone()
    if not n: return {"error": "note not found"}
    act = d["action"]
    if act in ("approve", "save"):
        b = json.loads(n["body_json"])
        b.update(headline=d["headline"], bullets=d["bullets"], exam_angle=d["exam_angle"])
        con.execute("UPDATE notes SET headline=?, body_json=? WHERE id=?", (d["headline"], json.dumps(b), n["id"]))
    if act == "approve":
        con.execute("UPDATE notes SET status='approved' WHERE id=?", (n["id"],))
        con.execute("UPDATE questions SET status='approved' WHERE note_id=? AND valid=1 AND solver_ok=1 AND status='pending'", (n["id"],))
    elif act in ("reject", "pending"):
        con.execute("UPDATE notes SET status=? WHERE id=?", ("rejected" if act == "reject" else "pending", n["id"]))
    con.commit()
    return {"ok": True}

def _post_editorial(con, d):
    a = d.get("action")
    if a == "save":
        if not d["title"].strip() or not d["body_md"].strip(): return {"error": "Title and body are required."}
        if d.get("id"): editorial.update(con, d["id"], title=d["title"], body_md=d["body_md"], day=d["day"], lenses=d["lenses"], status=d["status"])
        else: editorial.add(con, d["title"], d["body_md"], d["day"], d["lenses"], status=d["status"])
    elif a == "status": editorial.set_status(con, d["id"], d["status"])
    elif a == "delete": editorial.delete(con, d["id"])
    return {"ok": True}

def _gd_list(con, status):
    return [dict(id=g["id"], day=g["day"], sector=g["sector"], topic=g["topic"], context=g["context"], status=g["status"], source=g["source"],
                 for_points=json.loads(g["for_json"]), against_points=json.loads(g["against_json"]),
                 key_facts=json.loads(g["facts_json"]), opening=g["opening"], conclusion=g["conclusion"])
            for g in con.execute("SELECT * FROM gd_topics WHERE status=? ORDER BY day DESC, id DESC", (status,))]

def _post_gd(con, d):
    a, i = d["action"], d.get("id", 0)
    vals = (d.get("topic", ""), d.get("context", ""), json.dumps(d.get("for_points", [])), json.dumps(d.get("against_points", [])),
            json.dumps(d.get("key_facts", [])), d.get("opening", ""), d.get("conclusion", ""))
    if a == "create":
        if not vals[0].strip(): return {"error": "Topic is required."}
        con.execute("INSERT INTO gd_topics(day,sector,topic,context,for_json,against_json,facts_json,opening,conclusion,status,source) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'approved','admin')", (date.today().isoformat(), "Other") + vals)
    elif a == "delete":
        con.execute("DELETE FROM gd_topics WHERE id=?", (i,))
    else:
        if a in ("approve", "save"):
            con.execute("UPDATE gd_topics SET topic=?, context=?, for_json=?, against_json=?, facts_json=?, opening=?, conclusion=? WHERE id=?", vals + (i,))
        if a in ("approve", "reject", "pending"):
            con.execute("UPDATE gd_topics SET status=? WHERE id=?", ({"approve": "approved", "reject": "rejected", "pending": "pending"}[a], i))
    con.commit()
    return {"ok": True}

def _post_static(con, path, d, llm_factory=None):
    p = path.rsplit("/", 1)[1]
    if p == "item":
        a, i = d["action"], d.get("id", 0)
        if a in ("create_approved", "create_pending"):
            return {"id": static.add_item(con, d["cat"], d.get("data"), "approved" if a == "create_approved" else "pending",
                                          d.get("as_on") or None, d.get("source", ""), d.get("volatility"))}
        if a == "delete": static.delete_item(con, i); return {"ok": True}
        st = {"approve": "approved", "reject": "rejected", "pending": "pending", "save": None}[a]
        static.update_item(con, i, d.get("data"), st, d.get("as_on") or None, d.get("source"), d.get("volatility"), note=a)
        return {"ok": True}
    if p == "import": return static.import_csv(con, d["cat"], d.get("csv", ""), bool(d.get("approve")))
    if p == "cat": return {"slug": static.add_category(con, d.get("name"), d.get("columns") or [], d.get("kind", "table"), d.get("volatility", "stable"))}
    if p == "approve-all": return {"approved": static.approve_all(con, d["cat"])}
    if p == "seed": return {"added": static.seed_starter(con)}
    if p == "fill":
        llm = (llm_factory or get_llm)()
        r = static_ai.fill_next(con, llm, min(int(d.get("calls", 6)), 20), 25, date.today().isoformat())
        usage.log_usage(con, date.today().isoformat(), llm, "static-fill"); return r
    if p in ("draft", "rates-process"):
        llm = (llm_factory or get_llm)()
        if p == "draft":
            r = (static_ai.extract_from_text(con, llm, d["cat"], d.get("text", ""), d.get("url", "")) if d.get("mode") == "text"
                 else static_ai.draft_from_knowledge(con, llm, d["cat"], d.get("hint", ""), d.get("n", 25)))
        else:
            if len((d.get("text") or "").strip()) < 50: raise ValueError("Paste the RBI statement text first.")
            r = rates.process_text(con, llm, d["text"], d.get("url") or "pasted RBI statement")
        usage.log_usage(con, date.today().isoformat(), llm, "static")
        return r
    if p == "rates-review":
        rates.review(con, d["id"], d["action"]); return {"ok": True}
    raise ValueError("Unknown action.")

def _eq(a, b):
    return hmac.compare_digest((a or "").encode(), (b or "").encode())

def is_admin_authorized(headers, peer=None):
    """With GK_ADMIN_PASSWORD set, the X-Admin-Key header (or 'Authorization: Bearer <password>') must match.
    Without it, only requests from this machine are allowed. `peer` is the real socket address: the Host header alone can be forged."""
    pw = os.getenv("GK_ADMIN_PASSWORD")
    if not pw:
        host = (headers.get("Host") or "").split(":")[0]
        local_peer = peer is None or peer in ("127.0.0.1", "::1")
        return host in ("localhost", "127.0.0.1") and local_peer and not headers.get("X-Forwarded-For")
    auth = headers.get("Authorization") or ""
    return _eq(headers.get("X-Admin-Key"), pw) or (auth.startswith("Bearer ") and _eq(auth[7:], pw))

def handle_get(con, path, query="", ctx=None):
    qs = parse_qs(query)
    if path == "/api/admin/jobs":
        return 200, jobs.overview(con)
    if path.startswith("/api/admin/jobs/"):
        j = jobs.get(con, int(path.rsplit("/", 1)[1]) if path.rsplit("/", 1)[1].isdigit() else 0)
        return (200, j) if j else (404, {"error": "no such job"})
    if path == "/api/meta":
        c = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM notes GROUP BY status")}
        c["published_editorials"] = con.execute("SELECT COUNT(*) FROM editorials WHERE status='published'").fetchone()[0]
        return 200, {"lenses": {k: v["label"] for k, v in config.load_lenses().items()}, "counts": c}
    if path == "/api/notes":
        return 200, _notes(con, qs.get("status", ["pending"])[0], qs.get("route", ["all"])[0])
    if path == "/api/static/cats":
        cats = static.overview(con)
        for c in cats:
            c["hints"] = static_ai.HINTS.get(c["slug"], [])
            c["knowledge_ok"] = c["slug"] not in static_ai.TEXT_ONLY and c["volatility"] != "volatile"
        return 200, cats
    if path == "/api/static/rates":
        return 200, rates.list_updates(con)
    if path == "/api/static/items":
        try:
            return 200, static.list_items(con, qs.get("cat", [""])[0], qs.get("status", [None])[0] or None)
        except ValueError as ve:
            return 400, {"error": str(ve)}
    if path == "/api/static/due":
        return 200, static.due_for_review(con)
    if path == "/api/static/history":
        return 200, static.history(con, int(qs.get("id", ["0"])[0]))
    if path == "/api/gd":
        return 200, _gd_list(con, qs.get("status", ["pending"])[0])
    if path == "/api/editorials":
        return 200, [dict(id=e["id"], title=e["title"], body_md=e["body_md"], day=e["day"], status=e["status"], source=e["source"], flags=e["flags"],
                          lenses=[x for x in (e["lenses"] or "").split(",") if x])
                     for e in con.execute("SELECT * FROM editorials ORDER BY day DESC, id DESC")]
    return 404, {"error": "not found"}

def handle_post(con, path, d, llm_factory=None, ctx=None):
    if path in ("/api/admin/jobs", "/api/admin/fetch-daily"):
        # long runs happen in a background thread; the page polls /api/admin/jobs/<id> for the log
        kind = "daily" if path.endswith("fetch-daily") else d.get("kind", "")
        params = {k: v for k, v in (d.get("params") or {}).items() if k in ("day", "calls")}
        if params.get("day") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(params["day"])): return 400, {"error": "Day must be YYYY-MM-DD."}
        c = dict(ctx or {}); c.setdefault("llm_factory", llm_factory)
        jid, err = jobs.start(kind, params, c)
        return (409, {"error": err}) if err else (202, {"id": jid, "day": params.get("day") or jobs.today()})
    if path == "/api/note":
        return 200, _post_note(con, d)
    if path == "/api/question":
        con.execute("UPDATE questions SET status=? WHERE id=?", (d["status"], d["id"]))
        con.commit()
        return 200, {"ok": True}
    if path == "/api/editorial":
        return 200, _post_editorial(con, d)
    if path == "/api/gd":
        return 200, _post_gd(con, d)
    if path.startswith("/api/static/"):
        try:
            return 200, _post_static(con, path, d, llm_factory)
        except ValueError as ve:
            return 400, {"error": str(ve)}
    if path == "/api/gd/generate":
        _l = (llm_factory or get_llm)()
        made = gd_generate(con, _l, date.today().isoformat())
        usage.log_usage(con, date.today().isoformat(), _l, "gd")
        return 200, {"made": made}
    if path == "/api/editorial/draft":
        try:
            _l = (llm_factory or get_llm)()
            eid = draft_from_topic(con, _l, d.get("topic", ""), d.get("extra", ""))
            usage.log_usage(con, date.today().isoformat(), _l, "editorial")
            return 200, {"id": eid}
        except ValueError as ve:
            return 400, {"error": str(ve)}
    if path == "/api/gd/from-topic":
        try:
            _l = (llm_factory or get_llm)()
            gid = generate_for_topic(con, _l, d.get("topic", ""), d.get("extra", ""))
            usage.log_usage(con, date.today().isoformat(), _l, "gd-topic")
            return 200, {"id": gid}
        except ValueError as ve:
            return 400, {"error": str(ve)}
    return 404, {"error": "not found"}

def make_server(port=5050, db_path=None, site_dir=None, llm_factory=None):
    site_dir = Path(site_dir or config.ROOT / "site").resolve()
    page = _page()
    ctx = {"db_path": db_path, "site_dir": site_dir, "llm_factory": llm_factory}
    _c = db.connect(db_path); jobs.recover(_c); _c.close()

    def _auto(con):
        """Keep the public site in sync with every admin change (no separate publish step needed)."""
        try: build_site(con, site_dir)
        except Exception: pass

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers(); self.wfile.write(raw)
        def _guard(self, api):
            if (self.headers.get("Host") or "").split(":")[0] not in ("localhost", "127.0.0.1"):
                self._send(403, {"error": "bad host"}); return False
            pw = os.getenv("GK_ADMIN_PASSWORD")
            if api and pw and not is_admin_authorized(self.headers):
                self._send(401, {"error": "password required"}); return False
            return True
        def do_GET(self):
            u = urlparse(self.path)
            if not self._guard(u.path.startswith("/api/")): return
            con = db.connect(db_path)
            try:
                if u.path == "/": return self._send(200, page, "text/html")
                if u.path.startswith("/site/"):
                    f = (site_dir / (u.path[6:] or "index.html")).resolve()
                    if site_dir in f.parents and f.is_file():
                        return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                code, resp = handle_get(con, u.path, u.query, ctx)
                return self._send(code, resp)
            finally:
                con.close()
        def do_POST(self):
            u = urlparse(self.path)
            if not self._guard(True): return
            con = db.connect(db_path)
            try:
                d = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 500_000)) or b"{}")
                if u.path == "/api/build":
                    _, n, q, e = build_site(con, site_dir)
                    return self._send(200, {"notes": n, "questions": q, "editorials": e})
                code, resp = handle_post(con, u.path, d, llm_factory, ctx)
                if code == 200:
                    _auto(con)
                return self._send(code, resp)
            except Exception as ex:
                print(f"[admin] ERROR {u.path}: {ex}", file=sys.stderr, flush=True)
                traceback.print_exc(file=sys.stderr)
                self._send(500, {"error": "Something went wrong."})
            finally:
                con.close()
    return ThreadingHTTPServer(("127.0.0.1", port), H)

def serve(port=5050):
    srv = make_server(port)
    url = f"http://localhost:{port}/"
    print(f"GK Admin running at {url}  (Ctrl+C to stop)")
    try: webbrowser.open(url)
    except Exception: pass
    try: srv.serve_forever()
    except KeyboardInterrupt: print("stopped")
