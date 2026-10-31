"""Background jobs for the admin panel: run the daily workflow on the server without holding a web request open.

One job runs at a time (SQLite + the AI quota do not like parallel runs). Each job keeps a live log (everything the
pipeline prints) that the admin page polls. Jobs are stored in the `jobs` table so the history survives page reloads."""
import io, json, os, sys, threading, traceback
from datetime import datetime, timedelta, timezone
from . import db, config

_lock = threading.Lock()
_local = threading.local()
_live = {}                                    # job id -> {"log": StringIO} while running

def today():
    """The run day in India time (servers usually run on UTC, which is a day behind until 05:30 IST)."""
    off = int(os.getenv("GK_TZ_OFFSET_MIN", "330"))
    return (datetime.now(timezone.utc) + timedelta(minutes=off)).date().isoformat()

def _stamp():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"

class _Tee(io.TextIOBase):
    """Stands in for sys.stdout/stderr: lines printed inside a job thread also go to that job's log."""
    def __init__(self, real): self.real = real
    def write(self, s):
        buf = getattr(_local, "buf", None)
        if buf is not None: buf.write(s)
        return self.real.write(s)
    def flush(self): self.real.flush()
    def __getattr__(self, k): return getattr(self.real, k)

def _install_tee():
    if not isinstance(sys.stdout, _Tee): sys.stdout = _Tee(sys.stdout)
    if not isinstance(sys.stderr, _Tee): sys.stderr = _Tee(sys.stderr)

# ---------- the steps ----------

def _llm(ctx, cap=None):
    from .llm import get_llm
    f = ctx.get("llm_factory")
    return f() if f else get_llm(cap=cap)

def step_daily(con, ctx, p):
    from . import pipeline
    return pipeline.run_daily(p.get("day") or today(), con=con, llm=_llm(ctx, config.DAILY_LLM_CALL_CAP) if ctx.get("llm_factory") else None)

def step_gd(con, ctx, p):
    from . import gd, usage
    llm = _llm(ctx); made = gd.generate(con, llm, p.get("day") or today())
    usage.log_usage(con, p.get("day") or today(), llm, "gd")
    return {"gd_topics_drafted": made}

def step_static_fill(con, ctx, p):
    from . import static_ai, usage
    llm = _llm(ctx); r = static_ai.fill_next(con, llm, max(1, min(int(p.get("calls", 6)), 40)), 25, today())
    usage.log_usage(con, today(), llm, "static-fill")
    return r

def step_rates(con, ctx, p):
    from . import rates, usage
    llm = _llm(ctx); found = rates.watch(con, llm, today())
    usage.log_usage(con, today(), llm, "rates")
    return {"releases_read": len(found), "applied": sum(len(x["applied"]) for x in found), "waiting": sum(len(x["pending"]) for x in found)}

def step_build(con, ctx, p):
    from .site import build_site
    _, n, q, e = build_site(con, ctx["site_dir"])
    return {"notes": n, "questions": q, "editorials": e}

def step_publish(con, ctx, p):
    from . import publish
    return publish.push_data_js(con, f"Daily update {p.get('day') or today()} (admin panel)")

def step_everyday(con, ctx, p):
    """The whole everyday routine: news -> GD topics -> rebuild the site -> back up to GitHub (if set up)."""
    out = {}
    for name, fn in (("daily", step_daily), ("gd", step_gd), ("build", step_build)):
        print(f"\n=== {name} ===", flush=True)
        try: out[name] = fn(con, ctx, p)
        except Exception as e:
            out[name] = {"error": str(e)}; print(f"{name} failed: {e}", flush=True)
            if name == "daily": raise
    from . import publish
    if publish.configured():
        print("\n=== publish ===", flush=True)
        try: out["publish"] = step_publish(con, ctx, p)
        except Exception as e: out["publish"] = {"error": str(e)}; print(f"publish failed: {e}", flush=True)
    return out

KINDS = {
    "everyday": ("Everyday update (all steps)", "Fetch & write today's notes and quizzes, draft GD topics, rebuild the site, back up to GitHub.", step_everyday),
    "daily": ("News pipeline only", "Fetch feeds, score, write notes + quizzes, check RBI rates. New notes land in the Review queue.", step_daily),
    "gd": ("Draft GD topics", "Up to GK_GD_PER_RUN topics from approved notes (pending review).", step_gd),
    "static-fill": ("Fill Static GK", "AI-draft the next batches for stable Static GK sections (pending review).", step_static_fill),
    "rates": ("Check RBI rates", "Read any new RBI policy statement and update Banking Terms rates.", step_rates),
    "build": ("Rebuild site files", "Regenerate site/index.html and site/data.js on the server.", step_build),
    "publish": ("Back up to GitHub", "Commit the approved content as site/data.js (keeps the Vercel copy fresh and restores content after a restart).", step_publish),
}

# ---------- running ----------

def _row(r, with_log=False):
    d = dict(id=r["id"], kind=r["kind"], label=KINDS.get(r["kind"], (r["kind"],))[0], params=json.loads(r["params"] or "{}"),
             status=r["status"], started=r["started"], finished=r["finished"], result=json.loads(r["result"]) if r["result"] else None)
    if with_log:
        live = _live.get(r["id"])
        d["log"] = live["log"].getvalue() if live else (r["log"] or "")
    return d

def running():
    return next(iter(_live), None)

def start(kind, params, ctx):
    """Queue a job and start it now. Returns (job_id, None) or (None, error message)."""
    if kind not in KINDS: return None, "Unknown job."
    if not _lock.acquire(blocking=False):
        return None, f"Job #{running()} is still running. Wait for it to finish."
    try:
        con = db.connect(ctx.get("db_path"))
        jid = con.execute("INSERT INTO jobs(kind,params,status,started) VALUES(?,?,'running',?)",
                          (kind, json.dumps(params or {}), _stamp())).lastrowid
        con.commit(); con.close()
        _live[jid] = {"log": io.StringIO()}
        _install_tee()
        threading.Thread(target=_run, args=(jid, kind, params or {}, ctx), daemon=True, name=f"gk-job-{jid}").start()
        return jid, None
    except Exception:
        _lock.release(); raise

def _run(jid, kind, params, ctx):
    _local.buf = _live[jid]["log"]
    status, result = "done", None
    con = None
    try:
        print(f"[{_stamp()}] {KINDS[kind][0]} started", flush=True)
        con = db.connect(ctx.get("db_path"))
        result = KINDS[kind][2](con, ctx, params)
        if isinstance(result, dict) and result.get("stopped"): status = "partial"
        print(f"[{_stamp()}] finished: {json.dumps(result, default=str)[:2000]}", flush=True)
    except Exception as e:
        status, result = "failed", {"error": str(e)}
        traceback.print_exc()
    finally:
        log = _live[jid]["log"].getvalue()[-200_000:]
        try:
            c2 = con or db.connect(ctx.get("db_path"))
            c2.execute("UPDATE jobs SET status=?, finished=?, result=?, log=? WHERE id=?",
                       (status, _stamp(), json.dumps(result, default=str), log, jid)); c2.commit(); c2.close()
        except Exception as e:
            sys.__stderr__.write(f"[jobs] could not save job {jid}: {e}\n")
        _local.buf = None
        _live.pop(jid, None)
        _lock.release()

def recover(con):
    """At server start: jobs that were 'running' when the server stopped will never finish."""
    con.execute("UPDATE jobs SET status='interrupted', finished=? WHERE status='running'", (_stamp(),)); con.commit()

def overview(con):
    from . import publish, mailer
    last = con.execute("SELECT day, detail, created FROM runs WHERE stage='daily_done' ORDER BY id DESC LIMIT 1").fetchone()
    counts = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM notes GROUP BY status")}
    return dict(
        today=today(), running=running(),
        kinds=[dict(kind=k, label=v[0], help=v[1]) for k, v in KINDS.items()],
        recent=[_row(r) for r in con.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 25")],
        last_daily=dict(day=last["day"], at=last["created"], detail=last["detail"]) if last else None,
        pending=dict(notes=counts.get("pending", 0),
                     gd=con.execute("SELECT COUNT(*) FROM gd_topics WHERE status='pending'").fetchone()[0],
                     static=con.execute("SELECT COUNT(*) FROM static_items WHERE status='pending'").fetchone()[0],
                     rates=con.execute("SELECT COUNT(*) FROM rate_updates WHERE status='pending'").fetchone()[0]),
        setup=dict(github=publish.configured(), mail=mailer.provider(), provider=_provider_name(),
                   db_path=db.where(ctx_path(con)), users=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]))

def _provider_name():
    try:
        from .llm import choose_provider
        return choose_provider()
    except Exception as e:
        return f"unknown ({e})"

def ctx_path(con):
    """File behind a local SQLite connection; None for Turso (db.where then names the Turso host)."""
    if not hasattr(con, "row_factory"): return None
    try: return con.execute("PRAGMA database_list").fetchone()[2] or None
    except Exception: return None

def get(con, jid):
    r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return _row(r, with_log=True) if r else None
