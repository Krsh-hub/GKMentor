from . import db, fetch, cluster, analyze, verify, quiz, config, usage, rates, static_ai
from .llm import get_llm, CallBudgetExceeded

def run_daily(day, mock=False, sources_path=None, con=None, llm=None):
    con = con or db.connect()
    llm = llm or get_llm(mock, cap=config.DAILY_LLM_CALL_CAP)
    cfg = config.load_sources(sources_path)
    stats = {"day": day, "provider": llm.provider}
    try:
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
        print(f"fetched {stats['fetched']} new articles; scoring relevance...", flush=True)
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
        for note_id, note, arts in made:
            route = verify.verify_note(con, note_id, note, arts)
            stats["routes"][route] = stats["routes"].get(route, 0) + 1
            if stopped:
                continue                       # out of quota/budget: notes are verified, quizzes resume next run
            try:
                quiz.make_quiz(con, llm, note_id, note)
            except CallBudgetExceeded as e:
                db.log(con, day, "budget_stop", e); stats["stopped"] = stopped = str(e)
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
