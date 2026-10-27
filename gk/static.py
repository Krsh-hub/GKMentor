"""Static GK: reference tables and glossaries that stay on the site once approved. Every change keeps a history row."""
import csv, io, json, random, re
from datetime import date, timedelta

REVIEW_DAYS = {"stable": 365, "yearly": 365, "volatile": 30}
GLOSSARY_COLS = ["term", "definition", "example", "formula", "why_it_matters", "related", "current_value"]
DEFAULT_CATS = [  # slug, name, kind, columns, default volatility, sort
    ("banking-terms", "Banking Terms", "glossary", GLOSSARY_COLS, "stable", 5),
    ("countries", "Countries", "table", ["country", "capital", "currency", "continent"], "stable", 10),
    ("states", "States & UTs", "table", ["name", "capital", "type"], "stable", 20),
    ("important-days", "Important Days", "table", ["date", "day", "about", "organisation"], "stable", 30),
    ("books-authors", "Books & Authors", "table", ["book", "author", "genre"], "stable", 40),
    ("awards", "Awards & Honours", "table", ["award", "field", "given_by"], "yearly", 50),
    ("organisation-hq", "Organisations & HQ", "table", ["organisation", "headquarters", "founded"], "stable", 60),
    ("constitution", "Constitution", "table", ["kind", "number", "subject"], "stable", 70),
    ("important-bills", "Important Bills", "table", ["bill", "passed_year", "passed_by", "importance", "background"], "stable", 75),
    ("sobriquets", "Sobriquets", "table", ["sobriquet", "given_to", "type", "field", "why_known"], "stable", 77),
    ("schemes", "Government Schemes", "table", ["scheme", "start_date", "sector", "purpose", "place", "description"], "stable", 72),
    ("chief-justices", "Chief Justices of India", "table", ["chief_justice", "no", "appointed_year", "tenure", "major_decisions"], "yearly", 95),
    ("presidents", "Presidents of India", "table", ["president", "no", "took_office", "tenure", "major_decisions"], "yearly", 96),
    ("committees", "Committees & Commissions", "table", ["committee", "established", "functions", "current_head"], "yearly", 85),
    ("india-indices", "India's Rank in Indices", "table", ["index", "publisher", "year", "india_rank", "total_countries", "top_country"], "yearly", 80),
    ("union-cabinet", "Union Cabinet", "table", ["ministry", "minister", "since"], "volatile", 90),
]
DEFAULT_FILTER = {"sobriquets": "type", "schemes": "sector"}                             # filter chips on the site: All / Person / Place / Event / Plant / Animal
QUIZ_GROUP = {"sobriquets": "type"}                              # quiz wrong-answers come from the same group
DEFAULT_SORT = {"schemes": {"col": "start_date", "dir": "desc"}, "chief-justices": {"col": "no", "dir": "desc"}, "presidents": {"col": "no", "dir": "desc"}, "committees": {"col": "established", "dir": "desc"}, "important-bills": {"col": "passed_year", "dir": "desc"}}      # newest first; readers can click any column header to re-sort
QUIZ = {  # slug -> [(column to ask for, question template)]
    "countries": [("capital", "What is the capital of {country}?"), ("currency", "Which currency is used in {country}?")],
    "states": [("capital", "What is the capital of {name}?")],
    "important-days": [("day", "Which day is observed on {date}?")],
    "books-authors": [("author", "Who wrote '{book}'?")],
    "awards": [("given_by", "Who gives the {award}?")],
    "organisation-hq": [("headquarters", "Where is the headquarters of {organisation}?")],
    "constitution": [("subject", "What does {kind} {number} deal with?")],
    "india-indices": [("india_rank", "What was India's rank in the {index} ({year})?")],
    "banking-terms": [("term", "Which term matches this definition? {definition}")],
    "committees": [("established", "In which year was the {committee} established?")],
    "important-bills": [("passed_year", "In which year was the {bill} passed?")],
    "schemes": [("start_date", "When was the {scheme} launched?"), ("place", "Where was the {scheme} launched?")],
    "chief-justices": [("appointed_year", "In which year did {chief_justice} become Chief Justice of India?")],
    "presidents": [("took_office", "In which year did {president} become President of India?")],
    "sobriquets": [("given_to", "Who or what is known as '{sobriquet}'?"), ("sobriquet", "What is the sobriquet of {given_to}?")],
}

def lab(k):
    return k.replace("_", " ").title()

def norm_key(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")

def ensure_categories(con):
    for slug, name, kind, cols, vol, sort in DEFAULT_CATS:
        con.execute("INSERT OR IGNORE INTO static_cats(slug,name,kind,columns_json,volatility,sort) VALUES(?,?,?,?,?,?)",
                    (slug, name, kind, json.dumps(cols), vol, sort))
        r = con.execute("SELECT id, columns_json FROM static_cats WHERE slug=?", (slug,)).fetchone()
        have = json.loads(r["columns_json"]); missing = [c for c in cols if c not in have]
        if missing:                                                 # a category gained columns in an update: add them, keep existing data
            con.execute("UPDATE static_cats SET columns_json=? WHERE id=?", (json.dumps(have + missing), r["id"]))

def cat(con, slug):
    r = con.execute("SELECT * FROM static_cats WHERE slug=?", (slug,)).fetchone()
    if not r: raise ValueError("Unknown category.")
    return r

def add_category(con, name, columns, kind="table", volatility="stable"):
    name = (name or "").strip()
    cols = [norm_key(c) for c in columns if norm_key(c)]
    if not (2 <= len(name) <= 60): raise ValueError("Category name must be 2-60 characters.")
    if kind not in ("table", "glossary"): raise ValueError("Kind must be table or glossary.")
    if kind == "glossary": cols = GLOSSARY_COLS
    if not (2 <= len(cols) <= 8) or len(set(cols)) != len(cols): raise ValueError("Give 2 to 8 different column names.")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if con.execute("SELECT 1 FROM static_cats WHERE slug=?", (slug,)).fetchone(): raise ValueError("That category already exists.")
    con.execute("INSERT INTO static_cats(slug,name,kind,columns_json,volatility,sort) VALUES(?,?,?,?,?,200)",
                (slug, name, kind, json.dumps(cols), volatility if volatility in REVIEW_DAYS else "stable"))
    con.commit()
    return slug

def _due(as_on, vol):
    return (date.fromisoformat(as_on) + timedelta(days=REVIEW_DAYS.get(vol, 365))).isoformat()

def _item(r, c=None):
    return dict(id=r["id"], cat=c["slug"] if c else None, data=json.loads(r["data_json"]), status=r["status"], as_on=r["as_on"],
                source=r["source"], volatility=r["volatility"], review_due=r["review_due"], updated=r["updated"])

def _hist(con, item_id, old, new, note=""):
    con.execute("INSERT INTO static_history(item_id,old_json,new_json,note) VALUES(?,?,?,?)",
                (item_id, json.dumps(old) if old is not None else None, json.dumps(new), note))

def add_item(con, slug, data, status="pending", as_on=None, source="", volatility=None):
    c = cat(con, slug); cols = json.loads(c["columns_json"])
    clean = {k: str((data or {}).get(k, "") or "").strip() for k in cols}
    if not clean[cols[0]]: raise ValueError(f"'{lab(cols[0])}' is required.")
    key = (lambda d: d[cols[0]].lower()) if c["kind"] == "glossary" else (lambda d: tuple(d[k].lower() for k in cols))
    for r in con.execute("SELECT data_json FROM static_items WHERE cat_id=? AND status!='rejected'", (c["id"],)):
        if key(json.loads(r["data_json"])) == key(clean): raise ValueError("This entry already exists.")
    vol = volatility if volatility in REVIEW_DAYS else c["volatility"]
    as_on = as_on or date.today().isoformat()
    cur = con.execute("INSERT INTO static_items(cat_id,data_json,status,volatility,as_on,source,review_due) VALUES(?,?,?,?,?,?,?)",
                      (c["id"], json.dumps(clean), status, vol, as_on, source or "", _due(as_on, vol)))
    _hist(con, cur.lastrowid, None, dict(data=clean, status=status, as_on=as_on), "created")
    con.commit()
    return cur.lastrowid

def update_item(con, item_id, data=None, status=None, as_on=None, source=None, volatility=None, note=""):
    r = con.execute("SELECT i.*, c.columns_json FROM static_items i JOIN static_cats c ON c.id=i.cat_id WHERE i.id=?", (item_id,)).fetchone()
    if not r: raise ValueError("Entry not found.")
    cols, old = json.loads(r["columns_json"]), json.loads(r["data_json"])
    new = {k: str((data or old).get(k, old.get(k, "")) or "").strip() for k in cols}
    if not new[cols[0]]: raise ValueError(f"'{lab(cols[0])}' is required.")
    st, ao = status or r["status"], as_on or r["as_on"]
    vol = volatility if volatility in REVIEW_DAYS else r["volatility"]
    before, after = dict(data=old, status=r["status"], as_on=r["as_on"]), dict(data=new, status=st, as_on=ao)
    if before != after: _hist(con, item_id, before, after, note)
    con.execute("UPDATE static_items SET data_json=?, status=?, as_on=?, source=?, volatility=?, review_due=?, updated=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(new), st, ao, r["source"] if source is None else source, vol, _due(ao, vol), item_id))
    con.commit()

def delete_item(con, item_id):
    con.execute("DELETE FROM static_items WHERE id=?", (item_id,)); con.commit()

def history(con, item_id):
    return [dict(at=h["changed_at"], old=json.loads(h["old_json"]) if h["old_json"] else None, new=json.loads(h["new_json"]), note=h["note"])
            for h in con.execute("SELECT * FROM static_history WHERE item_id=? ORDER BY id DESC", (item_id,))]

def list_items(con, slug, status=None):
    c = cat(con, slug)
    q, a = "SELECT * FROM static_items WHERE cat_id=?", [c["id"]]
    if status: q += " AND status=?"; a.append(status)
    return [_item(r, c) for r in con.execute(q + " ORDER BY id", a)]

def due_for_review(con, today=None):
    rows = con.execute("SELECT i.*, c.slug, c.name FROM static_items i JOIN static_cats c ON c.id=i.cat_id WHERE i.status='approved' AND i.review_due<=? ORDER BY i.review_due",
                       (today or date.today().isoformat(),)).fetchall()
    return [dict(_item(r, dict(slug=r["slug"])), cat_name=r["name"]) for r in rows]

def approve_all(con, slug):
    n = 0
    for it in list_items(con, slug, "pending"):
        update_item(con, it["id"], status="approved", note="bulk approve"); n += 1
    return n

def overview(con):
    out = []
    for c in con.execute("SELECT * FROM static_cats ORDER BY sort, name"):
        counts = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM static_items WHERE cat_id=? GROUP BY status", (c["id"],))}
        out.append(dict(slug=c["slug"], name=c["name"], kind=c["kind"], columns=json.loads(c["columns_json"]), volatility=c["volatility"], counts=counts))
    return out

def import_csv(con, slug, text, approve=False):
    """Paste/upload a CSV whose header names match the category's columns. Existing entries are skipped."""
    c = cat(con, slug); cols = json.loads(c["columns_json"])
    rd = csv.DictReader(io.StringIO((text or "").lstrip("\ufeff")))
    heads = {norm_key(h): h for h in (rd.fieldnames or [])}
    res = dict(added=0, skipped=0, errors=[], ignored_columns=[h for k, h in heads.items() if k not in cols])
    if cols[0] not in heads:
        res["errors"].append(f"The header row must include '{cols[0]}'. Expected columns: {', '.join(cols)}.")
        return res
    for n, row in enumerate(rd, 2):
        if n > 5001: res["errors"].append("Stopped at 5000 rows."); break
        data = {k: row.get(heads[k], "") for k in cols if k in heads}
        try:
            add_item(con, slug, data, status="approved" if approve else "pending", source="CSV import")
            res["added"] += 1
        except ValueError as e:
            if "already exists" in str(e): res["skipped"] += 1
            else: res["errors"].append(f"Row {n}: {e}")
    return res

def seed_starter(con):
    """Load the starter set as PENDING entries (skips ones that already exist). Returns how many were added."""
    from .static_seed import SEED, SOURCE
    n = 0
    for slug, rows in SEED.items():
        cols = json.loads(cat(con, slug)["columns_json"])
        for row in rows:
            try:
                add_item(con, slug, dict(zip(cols, row)), status="pending", source=SOURCE); n += 1
            except ValueError:
                pass
    return n

def build_questions(c, items, per_cat=60):
    """Multiple-choice questions straight from approved rows (no AI). Fast-changing entries are left out."""
    cols = c["columns"]; slug = c["slug"]
    rows = [i for i in items if i["volatility"] != "volatile"]
    specs = QUIZ.get(slug) or ([(cols[1], f"What is the {lab(cols[1])} of {{{cols[0]}}}?")] if len(cols) > 1 and c["kind"] == "table" else [])
    rng, out = random.Random(f"{slug}-{len(rows)}"), []
    for ask, tpl in specs:
        have = [r for r in rows if r["data"].get(ask)]
        values = {r["data"][ask] for r in have}
        if len(values) < 4: continue
        for r in have:
            ans = r["data"][ask]
            same_key = {x["data"][ask] for x in have if x["data"][cols[0]] == r["data"][cols[0]]}          # other right answers to this very question
            g = QUIZ_GROUP.get(slug)
            pool = {x["data"][ask] for x in have if g and x["data"].get(g) == r["data"].get(g)} - same_key
            if len(pool) < 3: pool = values - same_key                                                      # too few in the group: use the whole column
            if len(pool) < 3: continue
            opts = rng.sample(sorted(pool), 3) + [ans]; rng.shuffle(opts)
            expl = " | ".join(f"{lab(k)}: {v}" for k, v in r["data"].items() if v)
            out.append(dict(q=tpl.format(**r["data"]), options=opts, answer=opts.index(ans), explanation=f"{expl} (as on {r['as_on']})",
                            cat=c["name"], static=1, sector="Other", day=""))
    rng.shuffle(out)
    return out[:per_cat]
