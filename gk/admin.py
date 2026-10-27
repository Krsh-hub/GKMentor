"""Local admin web app (no Streamlit). Standard library only. Binds to 127.0.0.1.
Set GK_ADMIN_PASSWORD to require a password for every API call."""
import json, os, re, webbrowser, mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from . import db, editorial, config
from .site import build_site
from . import usage, static, rates, static_ai
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
    out = []
    for n in con.execute(q, (status,) if route == "all" else (status, route)):
        b = json.loads(n["body_json"])
        qs = []
        for x in con.execute("SELECT * FROM questions WHERE note_id=?", (n["id"],)):
            m = json.loads(x["body_json"])
            qs.append(dict(id=x["id"], q=m["question"], options=m["options"], answer=m["answer_index"],
                           valid=x["valid"], solver_ok=x["solver_ok"], status=x["status"]))
        out.append(dict(id=n["id"], day=n["day"], sector=n["sector"], headline=n["headline"], importance=n["importance"],
                        route=n["route"], bullets=b["bullets"], exam_angle=b.get("exam_angle", ""),
                        sources=json.loads(n["source_urls"] or "[]"),
                        claims=[dict(kind=c["kind"], text=c["text"], grounded=c["grounded"])
                                for c in con.execute("SELECT * FROM claims WHERE note_id=?", (n["id"],))], questions=qs))
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

def make_server(port=5050, db_path=None, site_dir=None, llm_factory=None):
    site_dir = Path(site_dir or config.ROOT / "site").resolve()
    page = _page()

    def _auto(con):
        """Keep the public site in sync with every admin change (no separate publish step needed)."""
        try: build_site(con, site_dir)
        except Exception: pass

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def _guard(self, api):
            if (self.headers.get("Host") or "").split(":")[0] not in ("localhost", "127.0.0.1"):
                self._send(403, {"error": "bad host"}); return False
            pw = os.getenv("GK_ADMIN_PASSWORD")
            if api and pw and self.headers.get("X-Admin-Key") != pw:
                self._send(401, {"error": "password required"}); return False
            return True
        def do_GET(self):
            u = urlparse(self.path)
            if not self._guard(u.path.startswith("/api/")): return
            con = db.connect(db_path)
            try:
                if u.path == "/": return self._send(200, page, "text/html")
                if u.path == "/api/meta":
                    c = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM notes GROUP BY status")}
                    c["published_editorials"] = con.execute("SELECT COUNT(*) FROM editorials WHERE status='published'").fetchone()[0]
                    return self._send(200, {"lenses": {k: v["label"] for k, v in config.load_lenses().items()}, "counts": c})
                if u.path == "/api/notes":
                    qs = parse_qs(u.query)
                    return self._send(200, _notes(con, qs.get("status", ["pending"])[0], qs.get("route", ["all"])[0]))
                if u.path == "/api/static/cats":
                    cats = static.overview(con)
                    for c in cats:
                        c["hints"] = static_ai.HINTS.get(c["slug"], []); c["knowledge_ok"] = c["slug"] not in static_ai.TEXT_ONLY and c["volatility"] != "volatile"
                    return self._send(200, cats)
                if u.path == "/api/static/rates": return self._send(200, rates.list_updates(con))
                if u.path == "/api/static/items":
                    q = parse_qs(u.query)
                    try: return self._send(200, static.list_items(con, q.get("cat", [""])[0], q.get("status", [None])[0] or None))
                    except ValueError as ve: return self._send(400, {"error": str(ve)})
                if u.path == "/api/static/due": return self._send(200, static.due_for_review(con))
                if u.path == "/api/static/history": return self._send(200, static.history(con, int(parse_qs(u.query).get("id", ["0"])[0])))
                if u.path == "/api/gd":
                    return self._send(200, _gd_list(con, parse_qs(u.query).get("status", ["pending"])[0]))
                if u.path == "/api/editorials":
                    return self._send(200, [dict(id=e["id"], title=e["title"], body_md=e["body_md"], day=e["day"], status=e["status"], source=e["source"], flags=e["flags"],
                                                 lenses=[x for x in (e["lenses"] or "").split(",") if x])
                                            for e in con.execute("SELECT * FROM editorials ORDER BY day DESC, id DESC")])
                if u.path.startswith("/site/"):
                    f = (site_dir / (u.path[6:] or "index.html")).resolve()
                    if site_dir in f.parents and f.is_file():
                        return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                self._send(404, {"error": "not found"})
            finally:
                con.close()
        def do_POST(self):
            u = urlparse(self.path)
            if not self._guard(True): return
            con = db.connect(db_path)
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if u.path == "/api/note":
                    r = _post_note(con, d); _auto(con); return self._send(200, r)
                if u.path == "/api/question":
                    con.execute("UPDATE questions SET status=? WHERE id=?", (d["status"], d["id"])); con.commit()
                    _auto(con); return self._send(200, {"ok": True})
                if u.path == "/api/editorial":
                    r = _post_editorial(con, d); _auto(con); return self._send(200, r)
                if u.path == "/api/gd":
                    r = _post_gd(con, d); _auto(con); return self._send(200, r)
                if u.path.startswith("/api/static/"):
                    try:
                        r = _post_static(con, u.path, d, llm_factory)
                    except ValueError as ve:
                        return self._send(400, {"error": str(ve)})
                    _auto(con); return self._send(200, r)
                if u.path == "/api/gd/generate":
                    _l = (llm_factory or get_llm)()
                    made = gd_generate(con, _l, date.today().isoformat()); usage.log_usage(con, date.today().isoformat(), _l, "gd")
                    return self._send(200, {"made": made})
                if u.path == "/api/editorial/draft":
                    try:
                        _l = (llm_factory or get_llm)()
                        eid = draft_from_topic(con, _l, d.get("topic", ""), d.get("extra", "")); usage.log_usage(con, date.today().isoformat(), _l, "editorial")
                    except ValueError as ve:
                        return self._send(400, {"error": str(ve)})
                    return self._send(200, {"id": eid})
                if u.path == "/api/gd/from-topic":
                    try:
                        _l = (llm_factory or get_llm)()
                        gid = generate_for_topic(con, _l, d.get("topic", ""), d.get("extra", "")); usage.log_usage(con, date.today().isoformat(), _l, "gd-topic")
                    except ValueError as ve:
                        return self._send(400, {"error": str(ve)})
                    return self._send(200, {"id": gid})
                if u.path == "/api/build":
                    _, n, q, e = build_site(con, site_dir)
                    return self._send(200, {"notes": n, "questions": q, "editorials": e})
                self._send(404, {"error": "not found"})
            except Exception as ex:
                self._send(500, {"error": str(ex)})
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
