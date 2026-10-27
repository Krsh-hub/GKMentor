"""Gold-note regression test: re-run the note prompt on hand-approved cases after ANY prompt/model change.
Add your own cases to gold/*.json (article text + facts a good note must contain)."""
import json, re
from pathlib import Path
from . import prompts, config
from .schemas import Note, Claim
from .sectors import SECTORS
from .verify import claim_grounded, _norm

GROUNDED_MIN, RECALL_MIN = 0.95, 0.8

def load_cases(d=None):
    d = Path(d) if d else config.ROOT / "gold"
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]

def _nums(s):
    return set(re.findall(r"\d+(?:\.\d+)?", s.replace(",", "")))

def evaluate_case(llm, case, lenses=()):
    note = llm.structured(prompts.NOTE_PROMPT.format(sectors=", ".join(SECTORS), lenses=list(lenses), title=case["title"], text=case["article"]),
                          Note, tier="strong", system=prompts.NOTE_SYSTEM)
    body = " ".join([note.headline, *note.bullets, *[c.text for c in note.claims]])
    grounded = [claim_grounded(c, case["article"]) for c in note.claims]
    grounded_rate = sum(grounded) / len(grounded) if grounded else 0.0
    nb = _norm(body)
    recall = sum(_norm(f) in nb for f in case["expected_facts"]) / len(case["expected_facts"])
    invented = _nums(" ".join([note.headline, *note.bullets, *[c.text for c in note.claims]])) - _nums(case["article"])
    checks = {"grounded": grounded_rate >= GROUNDED_MIN, "recall": recall >= RECALL_MIN,
              "no_invented_numbers": not invented, "bullets_3_to_8": 3 <= len(note.bullets) <= 8}
    return {"id": case["id"], "grounded_rate": round(grounded_rate, 2), "recall": round(recall, 2),
            "invented_numbers": sorted(invented), "checks": checks, "passed": all(checks.values())}

def run_gold(llm, d=None, lenses=()):
    results = [evaluate_case(llm, c, lenses) for c in load_cases(d)]
    return {"results": results, "passed": all(r["passed"] for r in results), "n": len(results)}
