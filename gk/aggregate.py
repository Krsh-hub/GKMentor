"""Daily/weekly/monthly are DERIVED views of approved data + admin editorials. Nothing is re-summarised from scratch."""
import json
from collections import defaultdict
from datetime import date, timedelta
from . import config, editorial

PUBLISHED = "('approved','auto_published')"

def _notes(con, start, end, lens=None):
    rows = con.execute(f"SELECT * FROM notes WHERE status IN {PUBLISHED} AND day BETWEEN ? AND ? ORDER BY day, id",
                       (start, end)).fetchall()
    if not lens:
        return rows
    prof = config.load_lenses()[lens]
    return [n for n in rows if lens in (n["lenses"] or "").split(",") or n["sector"] in prof.get("sectors", [])]

def _render_note(n, b, lens, prof, grounded):
    out = [f"### {n['headline']}  \n*{n['day']} - {n['importance']}*"]
    lp = b.get("lens_points", {}).get(lens) if lens else None
    style = prof["style"] if prof else "analytical"
    if style == "factual":
        facts = grounded.get(n["id"], [])[: prof.get("max_facts", 6)]
        out += [f"- {f}" for f in facts] or [f"- {x}" for x in b["bullets"][:4]]
        if lp: out.append(f"**Remember:** {lp}")
    elif style == "discussion":
        out += [f"- {x}" for x in b["bullets"][: prof.get("max_bullets", 3)]]
        if lp: out.append(f"**Discussion point:** {lp}")
    else:
        out += [f"- {x}" for x in b["bullets"][: (prof or {}).get("max_bullets", 7)]]
        out.append(f"**Exam angle:** {b['exam_angle']}")
        if lp: out.append(f"**{(prof or {}).get('label', 'Lens')} angle:** {lp}")
        if b.get("static_links"): out.append("**Static links:** " + "; ".join(b["static_links"]))
    return "\n".join(out) + "\n"

def build_period(con, start, end, title, lens=None):
    prof = config.load_lenses()[lens] if lens else None
    notes = _notes(con, start, end, lens)
    ids = [n["id"] for n in notes]
    ph = ",".join("?" * len(ids)) or "NULL"
    grounded = defaultdict(list)
    for c in con.execute(f"SELECT note_id,text FROM claims WHERE grounded=1 AND note_id IN ({ph})", ids):
        grounded[c["note_id"]].append(c["text"])
    label = f" - {prof['label']}" if prof else ""
    lines = [f"# {title}{label}", f"_{start} to {end} - {len(notes)} approved notes_", ""]

    eds = editorial.for_range(con, start, end, lens)
    if eds:
        lines.append("## Editorial")
        for e in eds:
            lines += [f"### {e['title']}", f"*{e['author']} - {e['day']}*", "", e["body_md"], ""]

    must = [n for n in notes if n["importance"] == "High"]
    lines += ["## Must remember"] + [f"- {n['headline']} ({n['day']})" for n in must] + [""]
    by_sector = defaultdict(list)
    for n in notes:
        by_sector[n["sector"]].append(n)
    for sector, items in sorted(by_sector.items()):
        lines.append(f"## {sector}")
        lines += [_render_note(n, json.loads(n["body_json"]), lens, prof, grounded) for n in items]

    if ids:
        facts = con.execute(f"SELECT c.kind,c.text,n.day FROM claims c JOIN notes n ON n.id=c.note_id "
                            f"WHERE c.grounded=1 AND n.id IN ({ph}) ORDER BY c.kind", ids).fetchall()
        if facts:
            lines += ["## Verified facts table", "| Type | Fact | Date |", "|---|---|---|"]
            lines += [f"| {f['kind']} | {f['text']} | {f['day']} |" for f in facts]
        qs = con.execute(f"SELECT * FROM questions WHERE note_id IN ({ph}) AND status='approved'", ids).fetchall()
        if qs:
            lines += ["", "## Practice questions"]
            for i, q in enumerate(qs, 1):
                m = json.loads(q["body_json"])
                lines.append(f"{i}. {m['question']}")
                lines += [f"   - {chr(65+j)}. {o}" for j, o in enumerate(m["options"])]
                lines.append(f"   - **Answer:** {chr(65+m['answer_index'])}. {m['explanation']}")
    return "\n".join(lines)

def _write(name, md):
    config.OUT_DIR.mkdir(exist_ok=True)
    p = config.OUT_DIR / name
    p.write_text(md, encoding="utf-8")
    return p

def daily(con, day, lens=None):
    suf = f"_{lens}" if lens else ""
    return _write(f"daily_{day}{suf}.md", build_period(con, day, day, f"Daily digest {day}", lens))

def weekly(con, end_day, lens=None):
    start = (date.fromisoformat(end_day) - timedelta(days=6)).isoformat()
    suf = f"_{lens}" if lens else ""
    return _write(f"weekly_{end_day}{suf}.md", build_period(con, start, end_day, f"Weekly wrap ending {end_day}", lens))

def monthly(con, year, month, lens=None):
    start = date(year, month, 1)
    end = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).isoformat()
    suf = f"_{lens}" if lens else ""
    return _write(f"monthly_{year}-{month:02d}{suf}.md",
                  build_period(con, start.isoformat(), end, f"Monthly compilation {year}-{month:02d}", lens))
