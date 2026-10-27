"""Canonical sectors. The model's free-text labels are mapped onto this fixed list so every note lands in a menu tab."""
import re
from collections import Counter

SECTORS = ["Polity", "Economy", "Banking", "Environment", "S&T", "Defence", "IR", "Schemes",
           "Awards", "Sports", "Appointments", "Reports&Indices", "Business", "Other"]
_NORM = {re.sub(r"\s", "", s).lower(): s for s in SECTORS}
_RULES = [  # order matters: first match wins
    ("Defence", r"defen[cs]e|military|\barmy\b|\bnavy\b|air force|missile"),
    ("IR", r"international|foreign|diplomac|sanction|bilateral|summit|brics|treaty|relations|nato|\bun\b"),
    ("Polity", r"polity|politic|governance|constitution|parliament|election|supreme court|high court|judic|legislat|\bbill\b|\blaw\b|article \d+|\bucc\b|federal"),
    ("Banking", r"\bbank|\brbi\b|monetary|repo rate|nbfc|\bupi\b"),
    ("Economy", r"econom|inflation|\bgdp\b|fiscal|budget|\btax|\btrade\b|food prices|msme|financ"),
    ("Business", r"business|corporate|compan|merger|\bipo\b|startup|market"),
    ("Environment", r"environment|climate|ecolog|pollution|wildlife|biodiversity|carbon|el nino|emission"),
    ("S&T", r"science|tech|space|isro|\bai\b|quantum|satellite|semiconductor|cyber"),
    ("Schemes", r"scheme|yojana|mission|welfare|subsid"),
    ("Awards", r"award|prize|nobel"),
    ("Sports", r"sport|cricket|olympic|fifa"),
    ("Appointments", r"appointed|appoints|new chief"),
    ("Reports&Indices", r"\bindex\b|\bindices\b|ranking|\branked\b"),
]

def canon(*candidates):
    """First candidate (sector label, then fallbacks such as the headline) that maps to a real sector; else 'Other'."""
    for c in candidates:
        t = (c or "").strip()
        if not t: continue
        hit = _NORM.get(re.sub(r"\s", "", t).lower())
        if hit and hit != "Other": return hit
        low = t.lower()
        for sector, rx in _RULES:
            if re.search(rx, low): return sector
    return "Other"

def repair(con):
    """Re-map sectors of existing notes (fixes notes saved with free-text labels). Returns {sector: count}."""
    import json
    counts = Counter()
    for n in con.execute("SELECT id, cluster_id, sector, headline, body_json FROM notes").fetchall():
        art = con.execute("SELECT sector FROM articles WHERE cluster_id=? AND status='kept' ORDER BY tier ASC, length(text) DESC LIMIT 1",
                          (n["cluster_id"],)).fetchone()
        s = canon(n["sector"], art["sector"] if art else None, n["headline"])
        b = json.loads(n["body_json"]); b["sector"] = s
        con.execute("UPDATE notes SET sector=?, body_json=? WHERE id=?", (s, json.dumps(b), n["id"]))
        counts[s] += 1
    con.commit()
    return dict(counts)

def balanced_pick(items, limit):
    """items: (cluster_id, score, sector). Round-robin across sectors (best first inside each) so one busy
    sector cannot take every slot and Awards/Science/Economy etc. all get notes."""
    by = {}
    for cid, score, sec in items:
        by.setdefault(sec, []).append((score, cid))
    for v in by.values():
        v.sort(reverse=True)
    order = sorted(by, key=lambda x: -by[x][0][0])
    out = []
    while len(out) < limit and any(by.values()):
        for sec in order:
            if by[sec] and len(out) < limit:
                out.append(by[sec].pop(0)[1])
    return out
