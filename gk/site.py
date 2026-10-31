"""Build the reader-facing static site (site/index.html + site/data.js) from APPROVED notes only.
Host the folder anywhere (open index.html locally, GitHub Pages, Netlify, any web server)."""
import json
from datetime import datetime
from pathlib import Path
from . import config, static

# Menu sections -> model sectors. Edit freely: each key becomes a menu tab.
GROUPS = {
    "Economy": ["Economy", "Banking"],
    "Business & Corporate": ["Business"],
    "Polity & Administration": ["Polity", "Schemes"],
    "International": ["IR"],
    "Science & Tech": ["S&T"],
    "Environment": ["Environment"],
    "Defence": ["Defence"],
    "Awards & Sports": ["Awards", "Sports", "Appointments"],
    "Reports & Indices": ["Reports&Indices"],
    "Other": ["Other"],
}
PUB = "('approved','auto_published')"

def build_site_data(con):
    # one query per table (not per note): matters when the database is across the network (Turso)
    facts, reported = {}, {}
    for c in con.execute(f"SELECT note_id, kind, text FROM claims WHERE grounded=1 AND note_id IN (SELECT id FROM notes WHERE status IN {PUB}) ORDER BY id"):
        facts.setdefault(c["note_id"], []).append({"k": c["kind"], "t": c["text"]})
    for r in con.execute(f"SELECT cluster_id, MIN(COALESCE(pub_day, fetched_on)) AS first FROM articles WHERE cluster_id IN "
                         f"(SELECT cluster_id FROM notes WHERE status IN {PUB}) GROUP BY cluster_id"):
        reported[r["cluster_id"]] = r["first"]
    notes = []
    for n in con.execute(f"SELECT * FROM notes WHERE status IN {PUB} ORDER BY day DESC, id DESC"):
        b = json.loads(n["body_json"])
        first = reported.get(n["cluster_id"])
        notes.append(dict(id=n["id"], day=n["day"], sector=n["sector"], headline=n["headline"], bullets=b["bullets"],
                          exam_angle=b.get("exam_angle", ""), static_links=b.get("static_links", []),
                          lens_points=b.get("lens_points", {}), importance=n["importance"],
                          lenses=[x for x in (n["lenses"] or "").split(",") if x],
                          sources=json.loads(n["source_urls"] or "[]"), facts=facts.get(n["id"], []),
                          reported=first if first and first < n["day"] else None))
    qs = []
    for q in con.execute(f"SELECT q.id, q.note_id, q.body_json, n.day, n.sector FROM questions q JOIN notes n ON n.id=q.note_id "
                         f"WHERE q.status='approved' AND q.valid=1 AND n.status IN {PUB}"):
        m = json.loads(q["body_json"])
        qs.append(dict(id=q["id"], note_id=q["note_id"], day=q["day"], sector=q["sector"], q=m["question"], options=m["options"],
                       answer=m["answer_index"], explanation=m["explanation"], difficulty=m.get("difficulty", "medium")))
    eds = [dict(id=e["id"], day=e["day"], title=e["title"], body_md=e["body_md"], author=e["author"],
                lenses=[x for x in (e["lenses"] or "").split(",") if x])
           for e in con.execute("SELECT * FROM editorials WHERE status='published' ORDER BY day DESC, id DESC")]
    gd = [dict(id=g["id"], day=g["day"], sector=g["sector"], topic=g["topic"], context=g["context"],
               for_points=json.loads(g["for_json"]), against_points=json.loads(g["against_json"]),
               key_facts=json.loads(g["facts_json"]), opening=g["opening"], conclusion=g["conclusion"])
          for g in con.execute("SELECT * FROM gd_topics WHERE status='approved' ORDER BY day DESC, id DESC")]
    scats, squestions = [], []
    for c in static.overview(con):
        items = static.list_items(con, c["slug"], "approved")
        if not items: continue
        scats.append(dict(slug=c["slug"], name=c["name"], kind=c["kind"], columns=c["columns"], sort=static.DEFAULT_SORT.get(c["slug"]), filter=static.DEFAULT_FILTER.get(c["slug"]),
                          items=[dict(id=i["id"], data=i["data"], as_on=i["as_on"], volatility=i["volatility"], source=i["source"] if str(i["source"]).startswith("http") else "") for i in items]))
        squestions += static.build_questions(c, items)
    lenses = {k: {"label": v["label"], "sectors": v.get("sectors", [])} for k, v in config.load_lenses().items()}
    return dict(generated=datetime.now().strftime("%d %b %Y, %H:%M"), groups=GROUPS, lenses=lenses,
                notes=notes, questions=qs, editorials=eds, gd=gd, static=scats, static_questions=squestions)

_cache = {"ver": None, "at": 0.0, "data": None}

def cached_site_data(con, ttl=300):
    """build_site_data, reused until something is written (or `ttl` seconds pass, for writes made by other processes)."""
    import time
    from . import turso, db
    if not (db.remote() and type(con).__module__.endswith("turso")): return build_site_data(con)   # local SQLite: always fresh
    if _cache["data"] is not None and _cache["ver"] == turso.VERSION[0] and time.time() - _cache["at"] < ttl:
        return _cache["data"]
    ver = turso.VERSION[0]
    data = build_site_data(con)
    _cache.update(ver=ver, at=time.time(), data=data)
    return data

def build_site(con, out="site", title="GK Daily"):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    data = build_site_data(con)
    (out / "data.js").write_text("window.GK_DATA=" + json.dumps(data, ensure_ascii=False) + ";", encoding="utf-8")
    html = (Path(__file__).with_name("site_template.html")).read_text(encoding="utf-8").replace("__TITLE__", title)
    (out / "index.html").write_text(html, encoding="utf-8")
    import os
    from .users import POLICY
    (out / "privacy.html").write_text((Path(__file__).with_name("privacy_template.html")).read_text(encoding="utf-8")
                                      .replace("__POLICY__", POLICY).replace("__CONTACT__", os.getenv("GK_CONTACT_EMAIL", "Add your contact email (GK_CONTACT_EMAIL).")), encoding="utf-8")
    return out / "index.html", len(data["notes"]), len(data["questions"]), len(data["editorials"])

