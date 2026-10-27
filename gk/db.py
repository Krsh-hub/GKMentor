import sqlite3, time
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
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, day TEXT, stage TEXT, detail TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
"""

def connect(path=None):
    con = sqlite3.connect(path or config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")                          # better concurrent read/write
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(editorials)")}
    for col in ("source TEXT DEFAULT 'admin'", "flags TEXT DEFAULT ''"):       # upgrade databases created by older versions
        if col.split()[0] not in have:
            con.execute(f"ALTER TABLE editorials ADD COLUMN {col}")
    have = {r[1] for r in con.execute("PRAGMA table_info(articles)")}
    for col in ("pub_day TEXT", "event_age INTEGER"):
        if col.split()[0] not in have:
            con.execute(f"ALTER TABLE articles ADD COLUMN {col}")
    from . import static
    static.ensure_categories(con)
    # housekeeping: purge expired sessions (cheap, runs once per connect)
    con.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    con.commit()
    return con

def log(con, day, stage, detail):
    con.execute("INSERT INTO runs(day,stage,detail) VALUES(?,?,?)", (day, stage, str(detail)))
    con.commit()

