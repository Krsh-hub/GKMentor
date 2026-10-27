"""Admin-authored editorials. Written and edited by a human, never generated or altered by the pipeline.
lenses = comma-separated lens ids; empty means the editorial appears in every lens view."""

def add(con, title, body_md, day, lenses=(), author="admin", status="draft", source="admin", flags=""):
    cur = con.execute("INSERT INTO editorials(day,title,body_md,lenses,author,status,source,flags) VALUES(?,?,?,?,?,?,?,?)",
                      (day, title.strip(), body_md.strip(), ",".join(lenses), author, status, source, flags))
    con.commit()
    return cur.lastrowid

def update(con, ed_id, **fields):
    allowed = {"day", "title", "body_md", "lenses", "author", "status"}
    sets = {k: (",".join(v) if k == "lenses" and not isinstance(v, str) else v) for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k}=?" for k in sets)
    con.execute(f"UPDATE editorials SET {cols}, updated=CURRENT_TIMESTAMP WHERE id=?", (*sets.values(), ed_id))
    con.commit()

def set_status(con, ed_id, status):
    assert status in ("draft", "published", "archived")
    update(con, ed_id, status=status)

def delete(con, ed_id):
    con.execute("DELETE FROM editorials WHERE id=?", (ed_id,))
    con.commit()

def for_range(con, start, end, lens=None):
    """Published editorials in [start, end]; with a lens, keep untagged ones plus those tagged for it."""
    rows = con.execute("SELECT * FROM editorials WHERE status='published' AND day BETWEEN ? AND ? ORDER BY day, id",
                       (start, end)).fetchall()
    if not lens:
        return rows
    return [r for r in rows if not r["lenses"] or lens in r["lenses"].split(",")]

def draft_from_topic(con, llm, topic, extra="", day=None):
    """AI drafts an editorial from the admin's topic. ALWAYS saved as an unpublished draft; numbers not found in the
    admin's notes or related approved notes are flagged for the admin to verify."""
    import re
    from datetime import date
    from . import prompts, config
    from .gd import related_notes
    from .schemas import EditorialDraft
    topic, extra = (topic or "").strip(), (extra or "").strip()
    if not (5 <= len(topic) <= 200):
        raise ValueError("Enter a topic of 5 to 200 characters.")
    ctx = "\n".join(f"- {n['headline']}: " + "; ".join(b["bullets"][:3]) for _, _, n, b in related_notes(con, topic)) or "none"
    d = llm.structured(prompts.ED_PROMPT.format(topic=topic, extra=extra or "none", context=ctx), EditorialDraft,
                       tier=config.ED_TIER, system=prompts.ED_SYSTEM)
    nums = lambda s: set(re.findall(r"\d+(?:\.\d+)?", s.replace(",", "")))
    flags = ", ".join(sorted(nums(d.body_md + " " + d.title) - nums(topic + " " + extra + " " + ctx), key=float))
    return add(con, d.title, d.body_md, day or date.today().isoformat(), (), status="draft", source="ai-draft", flags=flags)
