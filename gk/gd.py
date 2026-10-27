"""GD topics: debate-ready topics built from APPROVED notes. AI drafts them (pending); you review, edit, approve."""
import json, re
from . import prompts, config, db
from .llm import CallBudgetExceeded
from .schemas import GDTopic
from .cluster import tokens
from .sectors import canon

def candidates(con, limit):
    return con.execute("SELECT * FROM notes WHERE status IN ('approved','auto_published') AND importance IN ('High','Medium') "
                       "AND id NOT IN (SELECT note_id FROM gd_topics WHERE note_id IS NOT NULL) "
                       "ORDER BY CASE importance WHEN 'High' THEN 0 ELSE 1 END, id DESC LIMIT ?", (limit,)).fetchall()

def _nums(s):
    return re.findall(r"\d+(?:\.\d+)?", s.replace(",", ""))

def generate(con, llm, day, limit=None):
    made = 0
    for n in candidates(con, limit or config.GD_PER_RUN):
        b = json.loads(n["body_json"])
        facts = [c["text"] for c in con.execute("SELECT text FROM claims WHERE note_id=? AND grounded=1", (n["id"],))]
        try:
            g = llm.structured(prompts.GD_PROMPT.format(headline=n["headline"], bullets="\n".join("- " + x for x in b["bullets"]),
                                                        facts="\n".join("- " + f for f in facts)),
                               GDTopic, tier=config.GD_TIER, system=prompts.GD_SYSTEM)
        except CallBudgetExceeded as e:
            db.log(con, day, "budget_stop", e); break
        except Exception as e:
            db.log(con, day, "gd_error", f"note {n['id']}: {e}"); continue
        src = (n["headline"] + " " + " ".join(b["bullets"]) + " " + " ".join(facts)).replace(",", "")
        kept = [f for f in g.key_facts if all(x in src for x in _nums(f))]          # drop facts with invented numbers
        con.execute("INSERT INTO gd_topics(note_id,day,sector,topic,context,for_json,against_json,facts_json,opening,conclusion) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (n["id"], n["day"], n["sector"], g.topic, g.context, json.dumps(g.for_points), json.dumps(g.against_points),
                     json.dumps(kept), g.opening, g.conclusion))
        con.commit(); made += 1
    return made

def related_notes(con, topic, k=3):
    """Approved notes that overlap with the editor's topic, used as verified context for the AI."""
    tt, scored = tokens(topic), []
    for n in con.execute("SELECT * FROM notes WHERE status IN ('approved','auto_published') ORDER BY id DESC LIMIT 400"):
        b = json.loads(n["body_json"])
        s = len(tt & tokens(n["headline"] + " " + " ".join(b["bullets"])))
        if s >= 2 or (s >= 1 and len(tt) <= 2):
            scored.append((s, n["id"], n, b))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return scored[:k]

def generate_for_topic(con, llm, topic, extra="", day=None):
    """Admin gives only a topic; the same GD structure is drafted (pending). Numbers must come from the admin's notes or approved notes."""
    from datetime import date
    topic, extra = (topic or "").strip(), (extra or "").strip()
    if not (5 <= len(topic) <= 200):
        raise ValueError("Enter a topic of 5 to 200 characters.")
    ctx = "\n".join(f"- {n['headline']}: " + "; ".join(b["bullets"][:3]) for _, _, n, b in related_notes(con, topic)) or "none"
    g = llm.structured(prompts.GD_TOPIC_PROMPT.format(topic=topic, extra=extra or "none", context=ctx),
                       GDTopic, tier=config.GD_TIER, system=prompts.GD_SYSTEM)
    src = (topic + " " + extra + " " + ctx).replace(",", "")
    kept = [f for f in g.key_facts if all(x in src for x in _nums(f))]
    cur = con.execute("INSERT INTO gd_topics(day,sector,topic,context,for_json,against_json,facts_json,opening,conclusion,status,source) "
                      "VALUES(?,?,?,?,?,?,?,?,?,'pending','admin-topic')",
                      (day or date.today().isoformat(), canon(topic), topic, g.context, json.dumps(g.for_points),
                       json.dumps(g.against_points), json.dumps(kept), g.opening, g.conclusion))
    con.commit()
    return cur.lastrowid
