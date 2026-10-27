"""CLI
  python run.py daily [--demo] [--day D]               run the pipeline
  python run.py daily-digest|weekly [--day D] [--lens L]   build a digest file from approved data + editorials
  python run.py monthly --year Y --month M [--lens L]
  python run.py editorial --title T --file body.md [--day D] [--lens L ...] [--status published]
  python run.py gold [--demo]                          regression-test the note prompt on gold/*.json
  python run.py admin [--port 5050]                    local admin site: review notes, editorials, publish
  python run.py serve [--port 5080] [--host 0.0.0.0]   public site server with user accounts (login/signup/Google)
  python run.py static-seed                            load the Static GK starter set (as pending, to verify)
  python run.py static-import --cat countries --file x.csv [--approve]   bulk-load a CSV (templates in data/static_templates)
  python run.py rates [--url <RBI release> | --file statement.txt]   read an RBI statement and update the Banking Terms rates
  python run.py static-draft --cat countries --hint Asia [--n 25]   AI-draft entries (pending);  or --file text.txt to extract from an official text
  python run.py static-fill [--calls 40]               AI-draft the next batches for ALL stable Static GK sections (pending)
  python run.py users                                  list signed-up users (no passwords are stored in readable form)
  python run.py gd [--demo]                            draft GD topics from approved notes (you review them in the admin site)
  python run.py coverage [--q nobel]                   where did a story go? per-source/section counts
  python run.py fix-sections                           re-map existing notes to the fixed menu sections
  python run.py build-site [--out site]                build the reader website from approved notes
  python run.py check-llm                              verify provider, key and model names with a tiny call
  python run.py approve-all                            demo helper"""
import argparse, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from datetime import date
from gk import db, pipeline, aggregate, config, editorial, gold
from gk.llm import get_llm

ap = argparse.ArgumentParser()
ap.add_argument("cmd", choices=["daily", "daily-digest", "weekly", "monthly", "editorial", "gold", "check-llm", "build-site", "fix-sections", "coverage", "gd", "serve", "users", "static-seed", "static-import", "rates", "static-draft", "static-fill", "admin", "approve-all"])
ap.add_argument("--day", default=date.today().isoformat())
ap.add_argument("--year", type=int); ap.add_argument("--month", type=int)
ap.add_argument("--lens", action="append", help="lens id (repeatable for editorial)")
ap.add_argument("--title"); ap.add_argument("--file"); ap.add_argument("--status", default="published")
ap.add_argument("--out", default="site"); ap.add_argument("--q"); ap.add_argument("--cat"); ap.add_argument("--url"); ap.add_argument("--hint", default=""); ap.add_argument("--n", type=int, default=25); ap.add_argument("--calls", type=int, default=40); ap.add_argument("--approve", action="store_true"); ap.add_argument("--port", type=int, default=None); ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--demo", action="store_true", help="offline: mock LLM + sample feeds")
a = ap.parse_args()
lens1 = a.lens[0] if a.lens else None

con = db.connect()
if a.cmd == "daily":
    src = str(config.ROOT / "data/demo/sources_demo.yaml") if a.demo else None
    print(pipeline.run_daily(a.day, mock=a.demo, sources_path=src))
elif a.cmd == "daily-digest":
    print(aggregate.daily(con, a.day, lens1))
elif a.cmd == "weekly":
    print(aggregate.weekly(con, a.day, lens1))
elif a.cmd == "monthly":
    print(aggregate.monthly(con, a.year or date.today().year, a.month or date.today().month, lens1))
elif a.cmd == "editorial":
    if not (a.title and a.file): sys.exit("editorial needs --title and --file")
    body = open(a.file, encoding="utf-8").read()
    print("editorial id", editorial.add(con, a.title, body, a.day, a.lens or (), status=a.status))
elif a.cmd == "gold":
    rep = gold.run_gold(get_llm(mock=a.demo), lenses=config.load_sources().get("lenses", []))
    for r in rep["results"]: print(("PASS " if r["passed"] else "FAIL ") + str(r))
    sys.exit(0 if rep["passed"] else 1)
elif a.cmd == "static-seed":
    from gk import static
    print("starter entries added (pending):", static.seed_starter(con))
elif a.cmd == "static-import":
    from gk import static
    if not (a.cat and a.file): sys.exit("static-import needs --cat <slug> and --file <csv>")
    print(static.import_csv(con, a.cat, open(a.file, encoding="utf-8-sig").read(), a.approve))
elif a.cmd == "static-fill":
    from gk import static_ai, usage
    _llm = get_llm(mock=a.demo)
    r = static_ai.fill_next(con, _llm, a.calls, 25, a.day)
    print(r)
    if _llm.provider != "mock": print(usage.line(usage.log_usage(con, a.day, _llm, "static-fill")))
elif a.cmd in ("rates", "static-draft"):
    from gk import rates, static_ai, fetch, usage
    _llm = get_llm(mock=a.demo)
    if a.cmd == "rates":
        if a.url or a.file:
            text = open(a.file, encoding="utf-8-sig").read() if a.file else (fetch.fulltext(a.url) or sys.exit("Could not fetch that page: save the statement text to a file and use --file."))
            print(rates.process_text(con, _llm, text, a.url or "pasted RBI statement", a.day))
        else:
            for x in rates.watch(con, _llm, a.day): print(x["title"][:80], "->", x["status"], x["applied"], x["pending"], x["problems"])
    else:
        if not a.cat: sys.exit("static-draft needs --cat <slug>")
        print(static_ai.extract_from_text(con, _llm, a.cat, open(a.file, encoding="utf-8-sig").read(), a.url or "") if a.file
              else static_ai.draft_from_knowledge(con, _llm, a.cat, a.hint, a.n))
    if _llm.provider != "mock": print(usage.line(usage.log_usage(con, a.day, _llm, a.cmd)))
elif a.cmd == "users":
    rows = con.execute("SELECT id,email,name,age_band,exams,plan,created,last_login FROM users ORDER BY id").fetchall()
    print(len(rows), "users")
    for r in rows:
        print(f"  #{r['id']} {r['email']} | {r['name']} | {r['age_band']} | exams: {r['exams'] or '-'} | {r['plan']} | joined {r['created']} | last login {r['last_login'] or '-'}".encode("ascii", "replace").decode())
elif a.cmd == "serve":
    from gk.serve import serve
    serve(a.port or 5080, a.host)
elif a.cmd == "gd":
    from gk.gd import generate
    _llm = get_llm(mock=a.demo)
    print("GD topics drafted:", generate(con, _llm, a.day))
    from gk import usage
    if _llm.provider != "mock": print(usage.line(usage.log_usage(con, a.day, _llm, "gd")))
elif a.cmd == "coverage":
    from gk.coverage import report
    print(report(con, a.q))
elif a.cmd == "fix-sections":
    from gk.sectors import repair
    print("notes per section:", repair(con))
elif a.cmd == "admin":
    from gk.admin import serve
    serve(a.port or 5050)
elif a.cmd == "build-site":
    from gk.site import build_site
    path, n, q, e = build_site(con, a.out)
    print(f"built {path}  ({n} notes, {q} questions, {e} editorials) - open it in your browser")
elif a.cmd == "check-llm":
    from gk.schemas import SolverAnswer
    llm = get_llm()
    print("provider:", llm.provider)
    if llm.provider == "mock": sys.exit("No API key detected - set ANTHROPIC_API_KEY or GEMINI_API_KEY")
    for tier in ("cheap", "strong"):
        try:
            r = llm.structured("QUESTION: What is 2+2?\nOPTIONS:\n0. 3\n1. 4", SolverAnswer, tier=tier, system="Answer the question.")
            print(f"{tier}: {llm.models[tier]} OK -> {r}")
        except Exception as e:
            print(f"{tier}: {llm.models[tier]} FAILED -> {e}")
else:
    con.execute("UPDATE notes SET status='approved' WHERE status='pending'")
    con.execute("UPDATE questions SET status='approved' WHERE status='pending'")
    con.commit(); print("approved all pending")
