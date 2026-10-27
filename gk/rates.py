"""Policy-rate watcher: reads official RBI statements and keeps the Banking Terms 'current value' fields up to date.
A rate is applied automatically ONLY if (1) it comes from an RBI (tier-1) source, (2) the AI's quoted sentence really is in the
official text and contains the number, (3) the value is in a sane range and (4) the rates agree with each other (SDF = repo - 0.25,
MSF = repo + 0.25, Bank Rate = MSF). Anything else is left PENDING for you to approve."""
import json, re
from datetime import date
from difflib import SequenceMatcher
from . import static, config, db, prompts, fetch
from .schemas import RateStatement
from .verify import _norm
from .llm import CallBudgetExceeded

RATE_TERMS = {"repo_rate": "repo rate", "reverse_repo_rate": "reverse repo", "sdf_rate": "sdf", "msf_rate": "msf",
              "bank_rate": "bank rate", "crr": "crr", "slr": "slr"}
RANGES = {"repo_rate": (1, 12), "reverse_repo_rate": (0.5, 12), "sdf_rate": (0.5, 12), "msf_rate": (1, 13),
          "bank_rate": (1, 13), "crr": (0.5, 12), "slr": (10, 30)}
TRIGGER = re.compile(r"monetary policy|\bMPC\b|repo rate|cash reserve ratio|\bCRR\b|statutory liquidity|\bSLR\b|policy rate|standing deposit|marginal standing", re.I)

def _grounded(quote, text):
    nq, nt = _norm(quote), _norm(text)
    if not nq: return False
    if nq in nt: return True
    return max((SequenceMatcher(None, nq, _norm(s)).ratio() for s in re.split(r"(?<=[.!?])\s+", text)), default=0) >= 0.9

def _value_in(value, quote):
    return any(abs(float(n) - value) < 1e-9 for n in re.findall(r"\d+(?:\.\d+)?", quote.replace(",", "")))

def fmt(v):
    return f"{v:.2f}%"

def validate(stmt, text):
    """Returns (checked {key: (value, quote)}, problems). Auto-apply needs ZERO problems."""
    ok, problems = {}, []
    for key, it in stmt.rates.items():
        if key not in RATE_TERMS: problems.append(f"unknown rate '{key}'"); continue
        lo, hi = RANGES[key]
        if not (lo <= it.value <= hi): problems.append(f"{key}={it.value} is outside the usual range"); continue
        if not _grounded(it.quote, text): problems.append(f"{key}: the quoted sentence is not in the official text"); continue
        if not _value_in(it.value, it.quote): problems.append(f"{key}: the quote does not contain {it.value}"); continue
        ok[key] = (it.value, it.quote)
    v = lambda k: ok[k][0] if k in ok else None
    if v("repo_rate") is not None and v("sdf_rate") is not None and abs(v("sdf_rate") - (v("repo_rate") - 0.25)) > 0.001: problems.append("SDF is not repo - 0.25")
    if v("repo_rate") is not None and v("msf_rate") is not None and abs(v("msf_rate") - (v("repo_rate") + 0.25)) > 0.001: problems.append("MSF is not repo + 0.25")
    if v("msf_rate") is not None and v("bank_rate") is not None and abs(v("msf_rate") - v("bank_rate")) > 0.001: problems.append("Bank Rate differs from MSF")
    return ok, problems

def _find(con, key):
    prefix, best = RATE_TERMS[key], None
    for it in static.list_items(con, "banking-terms"):
        if it["status"] != "rejected" and it["data"]["term"].lower().startswith(prefix):
            if it["status"] == "approved": return it
            best = best or it
    return best

def _apply(con, key, value, as_on, url, uid=None):
    it = _find(con, key)
    if not it: return None
    data = dict(it["data"]); old = data.get("current_value", "")
    if old == fmt(value) and it["as_on"] == as_on: return (it["data"]["term"], old, old)
    data["current_value"] = fmt(value)
    static.update_item(con, it["id"], data=data, as_on=as_on, source=url or "RBI statement", note="rate update from an RBI announcement")
    return (data["term"], old, data["current_value"])

def _valid_date(s):
    try:
        d = date.fromisoformat(s or "")
        return d.isoformat() if d <= date.today() else None
    except ValueError:
        return None

def process_text(con, llm, text, url="", day=None, auto=None):
    auto = config.RATES_AUTO if auto is None else auto
    day = day or date.today().isoformat()
    stmt = llm.structured(prompts.RATE_PROMPT.format(text=text[:12000]), RateStatement, tier="cheap", system=prompts.RATE_SYSTEM)
    res = dict(status="ignored", applied=[], pending=[], unchanged=[], problems=[])
    if not stmt.is_rate_announcement or not stmt.rates: return res
    ok, problems = validate(stmt, text)
    res["problems"] = list(problems)
    as_on = _valid_date(stmt.announced_on) or day
    for key, (value, quote) in ok.items():
        it = _find(con, key)
        if not it:
            res["problems"].append(f"no Banking Terms entry for {key}: add the term first"); continue
        old = it["data"].get("current_value", "")
        if old == fmt(value): res["unchanged"].append(it["data"]["term"]); continue
        con.execute("UPDATE rate_updates SET status='superseded' WHERE rate_key=? AND status='pending'", (key,))
        status = "applied" if (auto and not problems) else "pending"
        if status == "applied": _apply(con, key, value, as_on, url)
        con.execute("INSERT INTO rate_updates(rate_key,term,old_value,new_value,as_on,source_url,quote,status,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                    (key, it["data"]["term"], old, fmt(value), as_on, url, quote, status, "; ".join(problems)))
        res["applied" if status == "applied" else "pending"].append((it["data"]["term"], old, fmt(value)))
    con.commit()
    res["status"] = "applied" if res["applied"] else "pending" if res["pending"] else "unchanged"
    return res

def list_updates(con, status=None, limit=40):
    q, a = "SELECT * FROM rate_updates", []
    if status: q += " WHERE status=?"; a.append(status)
    return [dict(r) for r in con.execute(q + " ORDER BY id DESC LIMIT ?", a + [limit])]

def review(con, uid, action):
    r = con.execute("SELECT * FROM rate_updates WHERE id=?", (uid,)).fetchone()
    if not r or r["status"] != "pending": raise ValueError("That update is not waiting for review.")
    if action == "approve":
        _apply(con, r["rate_key"], float(r["new_value"].rstrip("%")), r["as_on"], r["source_url"])
        con.execute("UPDATE rate_updates SET status='applied' WHERE id=?", (uid,))
    else:
        con.execute("UPDATE rate_updates SET status='rejected' WHERE id=?", (uid,))
    con.commit()

def watch(con, llm, day, fetcher=None):
    """Checks new RBI (tier-1) releases that mention policy rates. Each release is processed once."""
    fetcher = fetcher or fetch.fulltext
    out = []
    rows = con.execute("SELECT * FROM articles WHERE tier=1 AND (source LIKE 'rbi%' OR url LIKE '%rbi.org.in%') AND fetched_on >= date(?, '-7 day') "
                       "AND url NOT IN (SELECT url FROM rate_seen)", (day,)).fetchall()
    for r in rows:
        if not TRIGGER.search(r["title"] + " " + (r["text"] or "")): 
            continue
        text = fetcher(r["url"]) or r["text"] or ""
        try:
            res = process_text(con, llm, text, r["url"], day)
        except CallBudgetExceeded:
            raise
        except Exception as e:
            db.log(con, day, "rates_error", f"{r['url']}: {e}"); continue
        con.execute("INSERT OR REPLACE INTO rate_seen(url,result) VALUES(?,?)", (r["url"], res["status"])); con.commit()
        out.append(dict(url=r["url"], title=r["title"], **res))
    return out
