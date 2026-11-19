from . import db, fetch, cluster, analyze, verify, quiz, config, usage, rates, static_ai
from .llm import get_llm, CallBudgetExceeded
from .schemas import Note

QUIZ_RESUME_DAYS = 3                                   # pending notes this recent that have no quiz get one on the next run

def _note(row):
    try: return Note.model_validate_json(row["body_json"])
    except Exception: return None                      # older/seeded notes have no claims etc.: nothing to resume

def unverified_notes(con):
    """Notes a crashed run stored but never verified (route still empty). Seeded notes have no prompt_version and are skipped."""
    out = []
    for r in con.execute("SELECT id, cluster_id, body_json FROM notes WHERE route IS NULL AND status='pending' "
                         "AND prompt_version IS NOT NULL ORDER BY id").fetchall():
        note = _note(r)
        arts = con.execute("SELECT * FROM articles WHERE cluster_id=? AND status='kept' ORDER BY tier ASC, length(text) DESC",
                           (r["cluster_id"],)).fetchall() if r["cluster_id"] is not None else []
        if note and arts: out.append((r["id"], note, arts))
    return out

def notes_without_quiz(con, day):
    """Recent pending notes (High/Medium) with no questions yet: this run's new notes plus any a crash or quota stop left behind."""
    rows = con.execute("SELECT id, body_json FROM notes WHERE status='pending' AND route IS NOT NULL AND importance != 'Low' "
                       "AND day >= date(?, ?) AND id NOT IN (SELECT DISTINCT note_id FROM questions WHERE note_id IS NOT NULL) ORDER BY id",
                       (day, f"-{QUIZ_RESUME_DAYS} day")).fetchall()
    return [(r["id"], n) for r in rows if (n := _note(r))]


def run_daily(day, mock=False, sources_path=None, con=None, llm=None):
    con = con or db.connect()
    llm = llm or get_llm(mock, cap=config.DAILY_LLM_CALL_CAP)
    cfg = config.load_sources(sources_path)
    stats = {"day": day, "provider": llm.provider}
    try:
        # articles a failed/stopped earlier run already saved but never scored: they are processed again below, not lost
        stats["carried_over"] = con.execute("SELECT COUNT(*) FROM articles WHERE status='new'").fetchone()[0]
        print("fetching feeds...", flush=True)
        stats["fetched"] = fetch.fetch_all(con, day, cfg)
        cluster.assign_clusters(con)
        stats["stale_dropped"] = fetch.drop_stale(con, day)
        try:                                                  # new RBI statement? keep the Banking Terms rates current
            found = rates.watch(con, llm, day)
            if found: stats["rate_updates"] = sum(len(x["applied"]) + len(x["pending"]) for x in found)
        except CallBudgetExceeded:
            raise
        except Exception as e:
            db.log(con, day, "rates_error", e)
        print(f"fetched {stats['fetched']} new articles (+{stats['carried_over']} left over from earlier runs); scoring relevance...", flush=True)
        if config.STATIC_AUTOFILL and llm.provider != "mock":     # keep filling the Static GK review queue a little each day
            try:
                f = static_ai.fill_next(con, llm, config.STATIC_AUTOFILL, 25, day)
                if f["calls"]: stats["static_drafted"] = f["added"]
            except CallBudgetExceeded:
                raise
            except Exception as e:
                db.log(con, day, "static_fill_error", e)
        stats["kept"] = analyze.score_articles(con, llm, day, cfg)
        try:
            stats["merged"] = analyze.merge_events(con, llm, day)
        except CallBudgetExceeded:
            raise
        except Exception as e:
            db.log(con, day, "merge_error", e)
        print("writing notes...", flush=True)
        made, stopped = analyze.build_notes(con, llm, day, cfg)
        if stopped:
            stats["stopped"] = stopped
        stats["notes"], stats["routes"] = len(made), {}
        mine = {m[0] for m in made}
        resumed = [x for x in unverified_notes(con) if x[0] not in mine]     # written by a run that crashed before checking them
        if resumed:
            stats["resumed_notes"] = len(resumed)
            print(f"finishing {len(resumed)} notes left unchecked by an earlier run...", flush=True)
        for note_id, note, arts in made + resumed:
            route = verify.verify_note(con, note_id, note, arts)
            stats["routes"][route] = stats["routes"].get(route, 0) + 1
        if not stopped:                                # out of quota/budget: notes are verified, quizzes resume next run
            for note_id, note in notes_without_quiz(con, day):
                try:
                    if quiz.make_quiz(con, llm, note_id, note): stats["quizzed"] = stats.get("quizzed", 0) + 1
                except CallBudgetExceeded as e:
                    db.log(con, day, "budget_stop", e); stats["stopped"] = str(e)
                    break
                except Exception as e:
                    db.log(con, day, "quiz_error", f"note {note_id}: {e}")
    except CallBudgetExceeded as e:
        db.log(con, day, "budget_stop", e)
        stats["stopped"] = str(e)
    stats["llm_calls"] = llm.calls
    if getattr(llm, "fell_back", False):
        stats["fallback"] = "strong model quota used up: notes were written with the cheap model"
    u = usage.log_usage(con, day, llm, "daily")
    stats["est_cost_usd"], stats["est_cost_inr"] = u["usd"], u["inr"]
    if llm.provider != "mock":
        print(usage.line(u), flush=True)
    db.log(con, day, "daily_done", stats)
    return stats
