import os, sqlite3, threading, time
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, source TEXT, tier INTEGER, title TEXT, published TEXT,
  text TEXT, fetched_on TEXT, cluster_id INTEGER, status TEXT DEFAULT 'new',
  rel_score INTEGER, sector TEXT, lenses TEXT, pub_day TEXT, event_age INTEGER);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, cluster_id INTEGER, day TEXT, sector TEXT, headline TEXT, body_json TEXT,
  importance TEXT, rel_score INTEGER, lenses TEXT, route TEXT, status TEXT DEFAULT 'pending',
  prompt_version TEXT, source_urls TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY, note_id INTEGER, text TEXT, kind TEXT, source_sentence TEXT,
  grounded INTEGER, corroborated INTEGER);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY, note_id INTEGER, body_json TEXT, valid INTEGER, solver_ok INTEGER,
  status TEXT DEFAULT 'pending', reports INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY, kind TEXT, text TEXT, note_id INTEGER, day TEXT);
CREATE TABLE IF NOT EXISTS editorials(
  id INTEGER PRIMARY KEY, day TEXT, title TEXT, body_md TEXT, lenses TEXT DEFAULT '',
  author TEXT DEFAULT 'admin', status TEXT DEFAULT 'draft', source TEXT DEFAULT 'admin', flags TEXT DEFAULT '',
  created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS gd_topics(
  id INTEGER PRIMARY KEY, note_id INTEGER, day TEXT, sector TEXT, topic TEXT, context TEXT,
  for_json TEXT, against_json TEXT, facts_json TEXT, opening TEXT, conclusion TEXT,
  status TEXT DEFAULT 'pending', source TEXT DEFAULT 'ai', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, pw_hash TEXT, google_sub TEXT UNIQUE,
  age_band TEXT, exams TEXT DEFAULT '', consent_version TEXT, consent_at TEXT, plan TEXT DEFAULT 'free',
  created TEXT DEFAULT CURRENT_TIMESTAMP, last_login TEXT);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
CREATE TABLE IF NOT EXISTS user_state(user_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(user_id, key));
CREATE TABLE IF NOT EXISTS login_attempts(
  email TEXT PRIMARY KEY, fails INTEGER DEFAULT 0, locked_until REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS static_cats(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, kind TEXT DEFAULT 'table', columns_json TEXT,
  volatility TEXT DEFAULT 'stable', sort INTEGER DEFAULT 100);
CREATE TABLE IF NOT EXISTS static_items(
  id INTEGER PRIMARY KEY, cat_id INTEGER, data_json TEXT, status TEXT DEFAULT 'pending', volatility TEXT DEFAULT 'stable',
  as_on TEXT, source TEXT DEFAULT '', review_due TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS static_history(
  id INTEGER PRIMARY KEY, item_id INTEGER, changed_at TEXT DEFAULT CURRENT_TIMESTAMP, old_json TEXT, new_json TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS rate_updates(
  id INTEGER PRIMARY KEY, detected_at TEXT DEFAULT CURRENT_TIMESTAMP, rate_key TEXT, term TEXT, old_value TEXT, new_value TEXT,
  as_on TEXT, source_url TEXT, quote TEXT, status TEXT, reason TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS rate_seen(url TEXT PRIMARY KEY, seen_at TEXT DEFAULT CURRENT_TIMESTAMP, result TEXT);
CREATE TABLE IF NOT EXISTS auth_tokens(
  token_hash TEXT PRIMARY KEY, user_id INTEGER, kind TEXT, expires REAL, created REAL);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT, params TEXT, status TEXT, started TEXT, finished TEXT, result TEXT, log TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, day TEXT, stage TEXT, detail TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
"""

_ready, _ready_lock, _swept = set(), threading.Lock(), [0.0]

def remote():
    """True when the app's main database is Turso (TURSO_DATABASE_URL set). An explicit path always means a local SQLite file."""
    from . import turso
    return turso.configured()

def where(path=None):
    """Human-readable location of the database, for the admin page."""
    if path is None and remote():
        from urllib.parse import urlparse
        return "Turso: " + urlparse(os.environ["TURSO_DATABASE_URL"].replace("libsql://", "https://", 1)).netloc
    return str(path or config.DB_PATH)

def connect(path=None):
    if path is None and remote():
        from . import turso
        con = turso.connect()
        key = "turso:" + os.environ["TURSO_DATABASE_URL"]
        with _ready_lock:                                       # schema check costs round trips: once per process
            if key not in _ready:
                setup(con); _ready.add(key)             # Turso keeps its data: no re-seeding (copy it once: python run.py turso-copy)
        if time.time() - _swept[0] > 600:                       # housekeeping at most every 10 minutes
            _swept[0] = time.time(); _sweep(con)
        return con
    con = sqlite3.connect(path or config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")                          # better concurrent read/write
    setup(con); _sweep(con)
    if path is None:
        seed_if_empty(con)
    return con

def setup(con):
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(editorials)")}
    for col in ("source TEXT DEFAULT 'admin'", "flags TEXT DEFAULT ''"):       # upgrade databases created by older versions
        if col.split()[0] not in have:
            con.execute(f"ALTER TABLE editorials ADD COLUMN {col}")
    have = {r[1] for r in con.execute("PRAGMA table_info(articles)")}
    for col in ("pub_day TEXT", "event_age INTEGER"):
        if col.split()[0] not in have:
            con.execute(f"ALTER TABLE articles ADD COLUMN {col}")
    have = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    if "email_verified" not in have:
        con.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
        con.execute("UPDATE users SET email_verified=1 WHERE google_sub IS NOT NULL")   # Google already verified these
    from . import static
    static.ensure_categories(con)
    con.commit()

def _sweep(con):
    """Purge expired sessions and email tokens."""
    con.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    con.execute("DELETE FROM auth_tokens WHERE expires < ?", (time.time(),))
    con.commit()

def copy_to_turso(src_path=None, replace=False, batch_bytes=800_000):
    """One-time move: copy every table of the local SQLite file (default gk.db) into the Turso database, keeping ids."""
    import json
    from . import turso
    if not remote(): raise SystemExit("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN first.")
    src = sqlite3.connect(src_path or config.DB_PATH); src.row_factory = sqlite3.Row
    dst = turso.connect(); setup(dst)
    tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    have = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # static_cats is filled automatically by setup(), so it is always overwritten
    busy = [t for t in tables if t in have and t != "static_cats" and dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]]
    if busy and not replace:
        raise SystemExit(f"Turso already has data in: {', '.join(busy)}. Re-run with --replace to wipe those tables and copy again.")
    report = {}
    for t in tables:
        if t not in have: continue                                   # table from an old version that the app no longer uses
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
        dcols = {r[1] for r in dst.execute(f"PRAGMA table_info({t})")}
        cols = [c for c in cols if c in dcols]
        if replace or t == "static_cats": dst.execute(f"DELETE FROM {t}")
        sql = f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        buf, size, n = [], 0, 0
        for r in src.execute(f"SELECT {','.join(cols)} FROM {t}"):
            vals = tuple(r); buf.append((sql, vals)); size += len(json.dumps(vals, default=str)) + 50; n += 1
            if size >= batch_bytes or len(buf) >= 500:
                dst.execute_many(buf); buf, size = [], 0
        if buf: dst.execute_many(buf)
        got = dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        report[t] = (n, got); print(f"  {t}: {n} rows copied, Turso now has {got}", flush=True)
    return report

def _note_for(con, q):
    """Older data.js files have no note_id on questions: attach them to a note of the same day and sector."""
    r = con.execute("SELECT id FROM notes WHERE day=? AND sector=? ORDER BY id LIMIT 1", (q.get("day"), q.get("sector"))).fetchone()
    return r["id"] if r else None

def seed_if_empty(con):
    try:
        if con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0:
            from . import static
            data_file = config.ROOT / "site/data.js"
            if data_file.exists():
                import json
                raw = data_file.read_text(encoding="utf-8")
                data = json.loads(raw.replace("window.GK_DATA=", "").rstrip(";"))
                for n in data.get("notes", []):
                    bj = json.dumps({"bullets": n.get("bullets", []), "exam_angle": n.get("exam_angle", ""), "static_links": n.get("static_links", []), "lens_points": n.get("lens_points", {})})
                    con.execute("INSERT OR IGNORE INTO notes(id, day, sector, headline, body_json, importance, lenses, status, source_urls) VALUES(?,?,?,?,?,?,?,'approved',?)",
                                (n["id"], n["day"], n["sector"], n["headline"], bj, n.get("importance", "Medium"), ",".join(n.get("lenses", [])), json.dumps(n.get("sources", []))))
                    for f in n.get("facts", []):
                        con.execute("INSERT INTO claims(note_id, text, kind, grounded) VALUES(?,?,?,1)", (n["id"], f.get("t", ""), f.get("k", "")))
                for q in data.get("questions", []):
                    bj = json.dumps({"question": q["q"], "options": q["options"], "answer_index": q["answer"], "explanation": q.get("explanation", ""), "difficulty": q.get("difficulty", "medium")})
                    con.execute("INSERT OR IGNORE INTO questions(id, note_id, body_json, valid, solver_ok, status) VALUES(?,?,?,1,1,'approved')",
                                (q["id"], q.get("note_id") or _note_for(con, q), bj))
                for e in data.get("editorials", []):
                    con.execute("INSERT OR IGNORE INTO editorials(id, day, title, body_md, author, lenses, status) VALUES(?,?,?,?,?,?,'published')",
                                (e["id"], e["day"], e["title"], e["body_md"], e.get("author", "admin"), ",".join(e.get("lenses", []))))
                for g in data.get("gd", []):
                    con.execute("INSERT OR IGNORE INTO gd_topics(id, day, sector, topic, context, for_json, against_json, facts_json, opening, conclusion, status) VALUES(?,?,?,?,?,?,?,?,?,?,'approved')",
                                (g["id"], g["day"], g["sector"], g["topic"], g["context"], json.dumps(g.get("for_points", [])), json.dumps(g.get("against_points", [])), json.dumps(g.get("key_facts", [])), g.get("opening", ""), g.get("conclusion", "")))
                for c in data.get("static", []):                       # approved Static GK entries come back approved
                    for it in c.get("items", []):
                        try: static.add_item(con, c["slug"], it.get("data"), "approved", it.get("as_on"), it.get("source", ""), it.get("volatility"))
                        except ValueError: pass                    # unknown category or already there
                con.commit()
            static.seed_starter(con)                             # fills any gaps as pending; skips rows restored above
    except Exception as e:
        import sys
        print(f"[db] seeding from site/data.js failed: {e}", file=sys.stderr)

def log(con, day, stage, detail):
    con.execute("INSERT INTO runs(day,stage,detail) VALUES(?,?,?)", (day, stage, str(detail)))
    con.commit()


