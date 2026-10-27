"""Fetch: RSS first. Failures are logged per source and never stop the run."""
import feedparser, re, html, email.utils
from datetime import datetime, timedelta, timezone
from . import db, config

AVOID_DEFAULT = []

def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def _ref(day):
    return datetime.fromisoformat(day) + timedelta(days=1)          # end of the run day

def _age_days(dt, day):
    if dt is None: return None
    if dt.tzinfo: dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return (_ref(day) - dt).total_seconds() / 86400

def _stored_dt(published):
    for f in (email.utils.parsedate_to_datetime, datetime.fromisoformat):
        try: return f(published)
        except Exception: pass
    return None

def drop_stale(con, day):
    """Mark old, unwritten articles as dropped so they never become 'today's' notes."""
    n = 0
    for r in con.execute("SELECT id, published FROM articles WHERE status IN ('new','kept') AND cluster_id NOT IN "
                         "(SELECT cluster_id FROM notes WHERE cluster_id IS NOT NULL) OR (status='new' AND cluster_id IS NULL)").fetchall():
        age = _age_days(_stored_dt(r["published"] or ""), day)
        if age is not None and age > config.MAX_AGE_DAYS:
            con.execute("UPDATE articles SET status='dropped' WHERE id=?", (r["id"],)); n += 1
    con.commit()
    return n

def fulltext(url):
    """Full text of a page (None if it cannot be fetched)."""
    return _fulltext(url)

def parse_feed(location):
    """location: URL, file path, or raw XML string."""
    return feedparser.parse(location)

def _fulltext(url):
    try:
        import trafilatura
        d = trafilatura.fetch_url(url)
        return trafilatura.extract(d) if d else None
    except Exception:
        return None

def fetch_all(con, day, cfg):
    avoid = [k.lower() for k in cfg.get("avoid_keywords", AVOID_DEFAULT)]
    new = 0
    for s in cfg["sources"]:
        if not s.get("enabled") or not s.get("rss"):
            continue
        try:
            feed = parse_feed(s["rss"])
            if feed.bozo and not feed.entries:
                raise RuntimeError(f"feed error: {feed.bozo_exception}")
            stale = 0
            for e in feed.entries:
                title, text = _clean(e.get("title")), _clean(e.get("summary") or e.get("description"))
                if any(k in (title + " " + text).lower() for k in avoid):
                    continue                                   # avoid-list as code
                pp = e.get("published_parsed") or e.get("updated_parsed")
                age = _age_days(datetime(*pp[:6]), day) if pp else None
                if age is not None and age > config.MAX_AGE_DAYS:
                    stale += 1
                    continue                                   # too old to be today's news
                if s.get("fulltext"):
                    text = _fulltext(e.link) or text
                cur = con.execute(
                    "INSERT OR IGNORE INTO articles(url,source,tier,title,published,text,fetched_on,pub_day) VALUES(?,?,?,?,?,?,?,?)",
                    (e.link, s["id"], s.get("tier", 2), title, e.get("published", ""), text, day, datetime(*pp[:6]).date().isoformat() if pp else None))
                new += cur.rowcount
            db.log(con, day, "fetch", f"{s['id']} ok ({len(feed.entries)} entries, {stale} too old)")
        except Exception as ex:
            db.log(con, day, "fetch_error", f"{s['id']}: {ex}")   # source-health signal
    con.commit()
    return new
