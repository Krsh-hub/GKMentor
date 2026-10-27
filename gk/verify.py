"""Trust layer: ground every claim in the source, require corroboration, route by confidence."""
import re, json
from difflib import SequenceMatcher
from . import config

CRITICAL = {"number", "date", "rank", "name"}

def _norm(s):
    s = re.sub(r"(?<=\d)\.(?=\d)", "_", s.lower().replace(",", ""))   # keep decimals intact: 61.2 -> 61_2
    return " ".join(re.sub(r"[^a-z0-9%_ ]+", " ", s).split())

def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]

def claim_grounded(claim, source_text):
    ns, nt = _norm(claim.source_sentence), _norm(source_text)
    if not ns:
        return False
    if ns not in nt:
        best = max((SequenceMatcher(None, ns, _norm(s)).ratio() for s in _sentences(source_text)), default=0)
        if best < 0.9:
            return False
    # every number in the claim must appear in the supporting sentence (catches digit slips)
    nums = re.findall(r"\d+(?:\.\d+)?", claim.text.replace(",", ""))
    return all(n.replace(".", "_") in ns for n in nums)

def corroborated(arts):
    return any(a["tier"] == 1 for a in arts) or len({a["source"] for a in arts}) >= 2

def verify_note(con, note_id, note, arts):
    """Returns the route: auto | quick_review | deep_review. Ungrounded claims are stored and flagged."""
    source_text = " ".join(a["text"] for a in arts)
    corr = corroborated(arts)
    bad_critical, bad_any = 0, 0
    for c in note.claims:
        g = claim_grounded(c, source_text)
        con_ = con.execute("INSERT INTO claims(note_id,text,kind,source_sentence,grounded,corroborated) VALUES(?,?,?,?,?,?)",
                           (note_id, c.text, c.kind, c.source_sentence, int(g), int(corr)))
        if not g:
            bad_any += 1
            bad_critical += c.kind in CRITICAL
    if bad_critical or not note.claims:
        route = "deep_review"
    elif bad_any or not corr:
        route = "quick_review"
    else:
        route = "auto"
    status = "auto_published" if (route == "auto" and config.AUTO_PUBLISH) else "pending"
    con.execute("UPDATE notes SET route=?, status=? WHERE id=?", (route, status, note_id))
    con.commit()
    return route

def valid_mcq(m):
    return (len(m.options) == 4 and len({o.strip().lower() for o in m.options}) == 4
            and 0 <= m.answer_index < 4 and bool(m.explanation.strip()))
