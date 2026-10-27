"""Group articles about the same event (token overlap; the AI duplicate check in analyze.merge_events catches the rest)."""
import re
from datetime import date, timedelta
from . import config

STOP = set("the a an of to in on for and with by at from as is are was be will after over new says".split())

def tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 1}

def sim(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    j = inter / len(ta | tb)
    if inter >= 5 and min(len(ta), len(tb)) >= 6:            # differently worded headlines of one story share a core
        return max(j, 0.85 * inter / min(len(ta), len(tb)))
    return j

def assign_clusters(con):
    rows = con.execute("SELECT id,title,cluster_id,fetched_on FROM articles ORDER BY id").fetchall()
    ref = max((r["fetched_on"] for r in rows if r["fetched_on"]), default=None)
    cutoff = (date.fromisoformat(ref) - timedelta(days=config.CLUSTER_WINDOW_DAYS)).isoformat() if ref else ""
    reps = {}                                                # cluster_id -> titles, recent clusters only
    nxt = max([r["cluster_id"] or 0 for r in rows] + [0]) + 1
    for r in rows:
        if r["cluster_id"] and (r["fetched_on"] or "") >= cutoff:
            reps.setdefault(r["cluster_id"], []).append(r["title"])
    for r in rows:
        if r["cluster_id"]:
            continue
        best, best_s = None, 0.0
        for cid, titles in reps.items():
            s = max(sim(r["title"], t) for t in titles)
            if s > best_s:
                best, best_s = cid, s
        if best and best_s >= config.CLUSTER_SIM:
            cid = best
            reps[cid].append(r["title"])
        else:
            cid, nxt = nxt, nxt + 1
            reps[cid] = [r["title"]]
        con.execute("UPDATE articles SET cluster_id=? WHERE id=?", (cid, r["id"]))
    con.commit()
