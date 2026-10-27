"""AI help for filling Static GK. Two safe modes:
 1. draft_from_knowledge: STABLE facts only (capitals, authors, HQs, articles...). Saved as PENDING, tagged 'verify'.
 2. extract_from_text: you paste an OFFICIAL text (a report, a minister list); only rows whose key and numbers appear in that text are kept.
Fast-changing facts (ministers, India's index ranks) are never drafted from the AI's memory."""
import re
from . import static, prompts, config, db
from .llm import CallBudgetExceeded
from .schemas import DraftRows

RECENT_BILLS = "2024 or later (the AI may be out of date: verify, or paste an official list)"
AUTO_SKIP = {("important-bills", RECENT_BILLS)}          # never drafted automatically: the AI's knowledge of the newest bills can be stale
GUIDE = {"important-bills": "bill = the official name of the Act or Bill; passed_year = the 4-digit year Parliament passed it (stay inside the focus years); "
                            "passed_by = who passed it: 'Parliament of India (<Prime Minister>'s government)'; importance = ONE short sentence on why it matters for exams; "
                            "background = at most two very short sentences. List the most recent bills first."}
GUIDE["schemes"] = ("scheme = the official scheme name; start_date = the launch date as 'D Month YYYY' (use 'Month YYYY' or 'YYYY' if you are not sure of the day); "
                    "sector = a short sector name such as Agriculture, Health, Education, Housing, Energy, Social security; purpose = ONE short sentence; "
                    "place = the launch venue or the state if the scheme is specific to one place, else empty; description = one or two short sentences on what it provides or does. "
                    "Only real central or major state schemes; never invent a scheme or a date.")
GUIDE["sobriquets"] = ("sobriquet = the nickname or epithet; given_to = the person, place, event, plant or animal it refers to; type = EXACTLY one of Person, Place, Event, Plant, Animal; "
                       "field = for a person their field (Politics, Sports, Literature, Science...), for a place its state or country, otherwise a short category; why_known = a few words or empty. "
                       "Only sobriquets widely accepted in Indian competitive exams; never invent one.")
HINTS = {
    "countries": ["Asia", "Europe", "Africa", "North America", "South America", "Oceania"],
    "states": ["States of India", "Union Territories of India"],
    "important-days": ["National days of India", "International days (UN)", "Health and environment days"],
    "books-authors": ["Ancient and medieval Indian texts", "Freedom-struggle era books", "Classic Indian fiction", "Classic world literature", "Autobiographies and non-fiction"],
    "awards": ["Indian civilian and gallantry awards", "Nobel and other international prizes", "Literary awards", "Sports awards and trophies"],
    "organisation-hq": ["UN agencies and their headquarters", "International financial institutions", "Indian regulators and commissions", "Defence and scientific organisations"],
    "constitution": ["Fundamental Rights articles", "Directive Principles and Fundamental Duties", "Parts of the Constitution", "Schedules", "Important amendments", "Constitutional bodies"],
    "important-bills": [RECENT_BILLS, "2019-2023", "2014-2018", "2004-2013", "1991-2003", "1950-1990"],
    "sobriquets": ["Freedom fighters and political leaders", "Sportspersons", "Writers, artists and scientists", "Cities and regions of India", "Places of the world", "Historical events and movements", "Plants, crops and trees", "Animals and birds"],
    "schemes": ["Agriculture and farmers", "Health and nutrition", "Education and skill development", "Women and child development", "Social security and pensions", "Rural development and employment", "Housing and urban development", "Financial inclusion and banking", "Energy, water and sanitation", "Industry, startups and MSMEs", "Digital, technology and infrastructure", "Defence, sports and others"],
    "committees": ["Constitutional bodies and commissions", "Statutory commissions and committees", "Important historical committees and commissions", "Parliamentary committees"],
    "banking-terms": ["Monetary policy and money market terms", "Banking regulation and supervision", "Payment systems and digital banking", "Loans, deposits and credit terms", "Financial markets and insurance basics"],
}
TEXT_ONLY = {"india-indices", "union-cabinet", "chief-justices", "presidents"}
TAG_KNOWLEDGE = "AI draft from general knowledge: verify before approving"

def knowledge_ok(c):
    return c["slug"] not in TEXT_ONLY and c["volatility"] != "volatile"

def _clean(cols, row):
    r = {static.norm_key(k): str(v or "").strip() for k, v in (row or {}).items()}
    return {k: r.get(k, "") for k in cols}

TYPES = {"person": "Person", "place": "Place", "event": "Event", "plant": "Plant", "animal": "Animal"}

def _year_ok(slug, d, hint):
    if slug == "schemes":                                      # a scheme needs a launch date that contains a real year
        return bool(d.get("scheme")) and bool(re.search(r"(1[89]\d\d|20\d\d)", d.get("start_date", "")))
    if slug == "sobriquets":                                   # type must be one of the five; normalise the capitalisation
        t = TYPES.get(d.get("type", "").strip().lower())
        if not t or not d.get("given_to"): return False
        d["type"] = t
        return True
    """Important Bills: the year must be a real 4-digit year inside the requested period (stops the AI drifting to other years)."""
    if slug != "important-bills": return True
    y = d.get("passed_year", "")
    if not re.fullmatch(r"\d{4}", y) or not (1950 <= int(y) <= 2100): return False
    m = re.search(r"(\d{4})\s*-\s*(\d{4})", hint or "") or re.search(r"(\d{4}) or later", hint or "")
    if not m: return True
    lo, hi = int(m.group(1)), (int(m.group(2)) if m.lastindex == 2 else 3000)
    return lo <= int(y) <= hi

def draft_from_knowledge(con, llm, slug, hint="", n=25):
    c = static.cat(con, slug)
    if not knowledge_ok(c):
        raise ValueError("Fast-changing or yearly-rank facts must come from an official text: paste it and use 'Extract from text'.")
    cols = __import__("json").loads(c["columns_json"])
    existing = [i["data"][cols[0]] for i in static.list_items(con, slug) if i["status"] != "rejected"]
    cap = 12 if c["kind"] == "glossary" else 40                       # glossary rows are long: keep the reply short enough
    res = llm.structured(prompts.DRAFT_PROMPT.format(n=min(max(int(n), 1), cap), name=c["name"], cols=", ".join(cols), hint=hint or "general coverage", guide=GUIDE.get(slug, "none"),
                                                     existing="; ".join(existing[-150:]) or "none"),
                         DraftRows, tier=config.STATIC_TIER, system=prompts.DRAFT_SYSTEM)
    out = dict(added=0, skipped=0, dropped=0)
    for row in res.rows:
        d = _clean(cols, row)
        if c["kind"] == "glossary": d["current_value"] = ""            # rates are only ever filled from RBI statements
        if "current_head" in d: d["current_head"] = ""                 # who heads a body changes: only you fill it, from the official source
        if not d[cols[0]] or not _year_ok(slug, d, hint): out["dropped"] += 1; continue
        try:
            static.add_item(con, slug, d, status="pending", source=TAG_KNOWLEDGE); out["added"] += 1
        except ValueError:
            out["skipped"] += 1
    return out

def extract_from_text(con, llm, slug, text, url=""):
    c = static.cat(con, slug)
    text = (text or "").strip()
    if len(text) < 50: raise ValueError("Paste the official text first (at least a few lines).")
    cols = __import__("json").loads(c["columns_json"])
    res = llm.structured(prompts.EXTRACT_PROMPT.format(name=c["name"], cols=", ".join(cols), text=text[:12000]),
                         DraftRows, tier=config.STATIC_TIER, system=prompts.DRAFT_SYSTEM)
    low, flat = text.lower(), text.replace(",", "")
    out = dict(added=0, skipped=0, dropped=0)
    for row in res.rows:
        d = _clean(cols, row)
        nums = [x.replace(",", "") for v in d.values() for x in re.findall(r"\d[\d,]*\.?\d*", v)]
        if not d[cols[0]] or d[cols[0]].lower() not in low or any(x not in flat for x in nums):
            out["dropped"] += 1; continue                                # not backed by the pasted text
        try:
            static.add_item(con, slug, d, status="pending", source=url or "Extracted from official text pasted by admin"); out["added"] += 1
        except ValueError:
            out["skipped"] += 1
    return out

FILL_ORDER = ["countries", "books-authors", "awards", "organisation-hq", "committees", "important-bills", "sobriquets", "schemes", "constitution", "important-days", "states", "banking-terms"]

def fill_next(con, llm, max_calls=2, n=30, day=None):
    """Drafts the next unfinished (category, focus) batches as PENDING entries. Each focus is done once; stops cleanly when the AI quota runs out."""
    from datetime import date
    day = day or date.today().isoformat()
    done = {r["detail"] for r in con.execute("SELECT detail FROM runs WHERE stage='static_fill'")}
    out = dict(calls=0, added=0, skipped=0, dropped=0, drafted=[], stopped=None, remaining=0)
    todo = [(slug, h) for slug in FILL_ORDER for h in HINTS.get(slug, []) if f"{slug}|{h}" not in done]
    for slug, hint in todo:
        c = static.cat(con, slug)
        if not knowledge_ok(c) or (slug, hint) in AUTO_SKIP: continue
        if out["calls"] >= max_calls: break
        try:
            r = draft_from_knowledge(con, llm, slug, hint, n)
        except CallBudgetExceeded as e:
            out["stopped"] = str(e); break
        except Exception as e:
            db.log(con, day, "static_fill_error", f"{slug}|{hint}: {e}"); continue      # not marked done: tried again next time
        db.log(con, day, "static_fill", f"{slug}|{hint}")
        out["calls"] += 1; out["added"] += r["added"]; out["skipped"] += r["skipped"]; out["dropped"] += r["dropped"]
        out["drafted"].append(f"{c['name']}: {hint} (+{r['added']})")
    out["remaining"] = max(0, len([1 for slug, h in todo if knowledge_ok(static.cat(con, slug))]) - out["calls"])
    return out
