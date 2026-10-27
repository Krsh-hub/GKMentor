"""Where did a story go?  python run.py coverage [--q nobel]"""
from collections import Counter
from .analyze import waiting_clusters

def _a(x):
    return str(x).encode("ascii", "replace").decode()

def report(con, q=None):
    L = ["ARTICLES BY SOURCE            total   new  kept  dropped"]
    for r in con.execute("SELECT source, COUNT(*) t, SUM(status='new') n, SUM(status='kept') k, SUM(status='dropped') d "
                         "FROM articles GROUP BY source ORDER BY t DESC"):
        L.append(f"  {r['source']:<26}{r['t']:>6}{r['n']:>6}{r['k']:>6}{r['d']:>8}")
    notes = {r[0]: r[1] for r in con.execute("SELECT sector, COUNT(*) FROM notes GROUP BY sector")}
    wait = Counter(sec for _, _, sec in waiting_clusters(con))
    L += ["", "BY SECTOR                    notes written   kept stories still waiting for a note"]
    for sec in sorted(set(notes) | set(wait)):
        L.append(f"  {sec:<26}{notes.get(sec, 0):>8}{wait.get(sec, 0):>20}")
    days = sorted({r[0] for r in con.execute("SELECT fetched_on FROM articles")} | {r[0] for r in con.execute("SELECT day FROM notes")}, reverse=True)[:10]
    L += ["", "BY DAY           fetched   kept  notes written  approved"]
    for d in days:
        one = lambda q: con.execute(q, (d,)).fetchone()[0]
        fetched = one("SELECT COUNT(*) FROM articles WHERE fetched_on=?")
        kept = one("SELECT COUNT(*) FROM articles WHERE fetched_on=? AND status='kept'")
        notes = one("SELECT COUNT(*) FROM notes WHERE day=?")
        ok = one("SELECT COUNT(*) FROM notes WHERE day=? AND status IN ('approved','auto_published')")
        L.append(f"  {d:<14}{fetched:>8}{kept:>7}{notes:>15}{ok:>10}")
    import json
    byday, fx = {}, None
    for r in con.execute("SELECT day, detail FROM runs WHERE stage='usage'"):
        try: u = json.loads(r["detail"])
        except Exception: continue
        d = byday.setdefault(r["day"], dict(calls=0, tin=0, tout=0, usd=0.0, fx=u.get("fx", 0)))
        d["calls"] += u.get("calls", 0); d["tin"] += u.get("tokens_in", 0); d["tout"] += u.get("tokens_out", 0); d["usd"] += u.get("usd", 0); d["fx"] = u.get("fx", d["fx"])
    if byday:
        L += ["", "AI USAGE (estimated cost if billed at paid-tier prices; Rs at the assumed rate in config/prices.yaml)",
              "  day          calls   tokens in   tokens out    USD      Rs"]
        for d in sorted(byday, reverse=True)[:7]:
            v = byday[d]
            L.append(f"  {d:<12}{v['calls']:>6}{v['tin']:>12,}{v['tout']:>13,}{v['usd']:>8.2f}{v['usd'] * v['fx']:>8.0f}")
        month = max(byday)[:7]
        mu = sum(v["usd"] for k, v in byday.items() if k.startswith(month)); mi = sum(v["usd"] * v["fx"] for k, v in byday.items() if k.startswith(month))
        L.append(f"  {month} so far: ${mu:.2f} (about Rs {mi:.0f})")
    L += ["", "RECENT RUNS (a 'stopped' entry means the AI quota ran out):"]
    for r in con.execute("SELECT created, detail FROM runs WHERE stage='daily_done' ORDER BY id DESC LIMIT 6"):
        L.append(f"  {str(r['created'])[:16]}  {_a(r['detail'])[:150]}")
    errs = [r[0] for r in con.execute("SELECT detail FROM runs WHERE stage IN ('fetch_error','score_error','note_error') ORDER BY id DESC LIMIT 8")]
    L += ["", "RECENT ERRORS (a feed listed here is not being fetched):"] + ([f"  {_a(e)[:140]}" for e in errs] or ["  none"])
    if q:
        L += ["", f"ARTICLES MATCHING '{q}':"]
        rows = con.execute("SELECT a.status, a.rel_score, a.sector, a.source, a.title, a.published, EXISTS(SELECT 1 FROM notes n WHERE n.cluster_id=a.cluster_id) hn "
                           "FROM articles a WHERE lower(a.title) LIKE ? ORDER BY a.id DESC LIMIT 25", (f"%{q.lower()}%",)).fetchall()
        for r in rows:
            L.append(f"  [{r['status']}/{r['rel_score']}/{r['sector']}] {r['source']} | {_a(r['published'])[:16]} | {_a(r['title'])[:80]} | note: {'yes' if r['hn'] else 'no'}")
        if not rows:
            L.append("  Nothing fetched with that word: the feeds do not carry it. Add a source in config/sources.yaml.")
        elif any(r["status"] == "new" for r in rows):
            L.append("  Status 'new' = not scored yet. Run: python run.py daily")
    return "\n".join(L)
