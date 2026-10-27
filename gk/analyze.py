import json
from . import prompts, config, db
from .llm import CallBudgetExceeded
from .schemas import RelevanceBatch, Note, NoteBatch, EventGroups
from .sectors import SECTORS, canon, balanced_pick

def score_articles(con, llm, day, cfg):
    """Batch relevance scoring. Articles that fail stay status='new' and are retried on the next run."""
    rows = con.execute("SELECT * FROM articles WHERE status='new' ORDER BY tier ASC, id DESC LIMIT ?",
                       (config.MAX_ARTICLES_PER_RUN,)).fetchall()
    size, kept = config.RELEVANCE_BATCH, 0
    for i in range(0, len(rows), size):
        batch = rows[i:i + size]
        items = "\n".join(f"[{r['id']}] (tier {r['tier']}, published {r['pub_day'] or r['fetched_on']}) {r['title']} :: {r['text'][:300]}" for r in batch)
        try:
            res = llm.structured(prompts.RELEVANCE_BATCH_PROMPT.format(today=day, lenses=cfg.get("lenses", []), items=items),
                                 RelevanceBatch, tier="cheap", system=prompts.RELEVANCE_SYSTEM)
        except CallBudgetExceeded:
            raise
        except Exception as e:
            db.log(con, day, "score_error", e)
            continue
        by_id = {it.id: it for it in res.items}
        for r in batch:
            it = by_id.get(r["id"])
            if it is None:
                continue                                  # model skipped it: stays 'new', retried next run
            old = it.event_age_days is not None and it.event_age_days > config.MAX_EVENT_AGE_DAYS      # old event, republished today
            ok = it.keep and it.score >= config.RELEVANCE_THRESHOLD and not old
            con.execute("UPDATE articles SET status=?, rel_score=?, sector=?, lenses=?, event_age=? WHERE id=?",
                        ("kept" if ok else "dropped", it.score, canon(it.sector, r["title"]), ",".join(it.lenses), it.event_age_days, r["id"]))
            kept += ok
        con.commit()
        print(f"  scored {min(i + size, len(rows))}/{len(rows)} articles", flush=True)
    return kept

def waiting_clusters(con):
    """Kept event clusters that have no note yet, as (cluster_id, score, sector)."""
    out = []
    for r in con.execute("SELECT cluster_id, MAX(rel_score) s FROM articles WHERE status='kept' AND cluster_id IS NOT NULL AND cluster_id NOT IN "
                         "(SELECT cluster_id FROM notes WHERE cluster_id IS NOT NULL) GROUP BY cluster_id"):
        b = con.execute("SELECT sector, title FROM articles WHERE cluster_id=? AND status='kept' ORDER BY rel_score DESC, tier ASC LIMIT 1",
                        (r["cluster_id"],)).fetchone()
        out.append((r["cluster_id"], r["s"], canon(b["sector"], b["title"])))
    return out

def merge_events(con, llm, day):
    """One cheap AI call groups headlines that are the same event (incl. ones already covered in recent notes), so a story gets ONE note."""
    waiting = con.execute("SELECT cluster_id cid, MAX(rel_score) s FROM articles WHERE status='kept' AND cluster_id IS NOT NULL AND cluster_id NOT IN "
                          "(SELECT cluster_id FROM notes WHERE cluster_id IS NOT NULL) GROUP BY cluster_id ORDER BY s DESC LIMIT 80").fetchall()
    covered = con.execute("SELECT cluster_id cid, headline FROM notes WHERE cluster_id IS NOT NULL AND day >= date(?, ?) ORDER BY id DESC LIMIT 100",
                          (day, f"-{config.EVENT_MERGE_DAYS} day")).fetchall()
    if not waiting or len(waiting) + len(covered) < 2:
        return 0
    title = lambda cid: con.execute("SELECT title FROM articles WHERE cluster_id=? AND status='kept' ORDER BY rel_score DESC, tier ASC LIMIT 1", (cid,)).fetchone()[0]
    lines = [f"[{r['cid']}] {title(r['cid'])}" for r in waiting] + [f"[{r['cid']}] (already covered) {r['headline']}" for r in covered]
    res = llm.structured(prompts.EVENT_PROMPT.format(items="\n".join(lines)), EventGroups, tier="cheap", system=prompts.RELEVANCE_SYSTEM)
    w, c, merged = {r["cid"] for r in waiting}, {r["cid"] for r in covered}, 0
    for g in res.groups:
        ids = [i for i in dict.fromkeys(g) if i in w or i in c]
        if len(ids) < 2:
            continue
        cov = [i for i in ids if i in c]
        target = cov[0] if cov else min(ids)
        for i in ids:
            if i != target and i in w:
                con.execute("UPDATE articles SET cluster_id=? WHERE cluster_id=?", (target, i)); merged += 1
    con.commit()
    return merged

def _source_text(arts):
    if len(arts) == 1:
        return arts[0]["text"][:6000]
    return "\n\n".join(f"SOURCE {i} ({a['source']}):\n{a['text'][:2000]}" for i, a in enumerate(arts[:3], 1))[:6000]

def _store_note(con, day, cid, arts, note):
    best = arts[0]
    note.sector = canon(note.sector, best["sector"], best["title"])      # always one of the fixed sectors
    cur = con.execute(
        "INSERT INTO notes(cluster_id,day,sector,headline,body_json,importance,rel_score,lenses,prompt_version,source_urls)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (cid, day, note.sector, note.headline, note.model_dump_json(), note.importance,
         best["rel_score"], best["lenses"], config.PROMPT_VERSION, json.dumps([a["url"] for a in arts])))
    con.commit()
    return cur.lastrowid

def _write(llm, group, cfg):
    """group: [(cluster_id, arts)]. One AI request writes up to NOTE_BATCH notes. Returns {position: Note}."""
    common = dict(sectors=", ".join(SECTORS), lenses=(cfg or {}).get("lenses", []))
    if len(group) == 1:
        arts = group[0][1]
        return {1: llm.structured(prompts.NOTE_PROMPT.format(title=arts[0]["title"], text=_source_text(arts), **common),
                                  Note, tier="strong", system=prompts.NOTE_SYSTEM)}
    events = "\n\n".join(f"=== EVENT {n} ===\nTITLE: {arts[0]['title']}\nSOURCES:\n{_source_text(arts)}" for n, (_, arts) in enumerate(group, 1))
    res = llm.structured(prompts.NOTE_BATCH_PROMPT.format(events=events, **common), NoteBatch, tier="strong", system=prompts.NOTE_SYSTEM)
    return {b.event: Note(**b.model_dump(exclude={"event"})) for b in res.notes}

def build_notes(con, llm, day, cfg=None):
    """Writes notes for the best waiting events (balanced across sectors), NOTE_BATCH events per AI request.
    Returns (made, stopped_reason). On a quota stop, notes already written are returned so they get verified; the rest resume next run."""
    clusters = balanced_pick(waiting_clusters(con), config.MAX_NOTES_PER_DAY)
    todo = []
    for cid in clusters:
        arts = con.execute("SELECT * FROM articles WHERE cluster_id=? AND status='kept' ORDER BY tier ASC, length(text) DESC", (cid,)).fetchall()
        if arts and not con.execute("SELECT 1 FROM notes WHERE cluster_id=?", (cid,)).fetchone():
            todo.append((cid, arts))
    made, stopped, size = [], None, max(1, min(config.NOTE_BATCH, 4))
    for i in range(0, len(todo), size):
        group = todo[i:i + size]
        try:
            notes = _write(llm, group, cfg)
        except CallBudgetExceeded as e:
            db.log(con, day, "budget_stop", e)
            stopped = str(e)
            break
        except Exception as e:
            db.log(con, day, "note_error", f"clusters {[c for c, _ in group]}: {e}")
            continue
        for n, (cid, arts) in enumerate(group, 1):
            note = notes.get(n)
            if note is None:
                db.log(con, day, "note_missing", f"cluster {cid}: left for the next run")
                continue
            made.append((_store_note(con, day, cid, arts, note), note, arts))
            print(f"  wrote note {len(made)}: {note.headline[:70]}", flush=True)
    return made, stopped
