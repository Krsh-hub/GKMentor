"""Token usage and estimated cost (what the same usage would cost on a paid plan)."""
import json, os, yaml
from . import config, db

def load_prices():
    p = config.ROOT / "config" / "prices.yaml"
    d = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    return d.get("models", {}) or {}, float(os.getenv("GK_USD_INR") or d.get("usd_inr", 90))

def summarize(llm):
    prices, fx = load_prices()
    tin = tout = 0
    usd, unknown = 0.0, []
    for model, (i, o) in llm.usage.items():
        tin += i; tout += o
        if model in prices: usd += i / 1e6 * prices[model][0] + o / 1e6 * prices[model][1]
        else: unknown.append(model)
    return dict(calls=llm.calls, tokens_in=tin, tokens_out=tout, usd=round(usd, 4), inr=round(usd * fx, 2), fx=fx,
                unknown=unknown, models={m: list(v) for m, v in llm.usage.items()})

def line(s):
    extra = f" (no price set for: {', '.join(s['unknown'])})" if s["unknown"] else ""
    return (f"AI usage: {s['calls']} calls, {s['tokens_in']:,} tokens in / {s['tokens_out']:,} out. Estimated cost on a paid plan: "
            f"${s['usd']:.2f} (about Rs {s['inr']:.0f} at an assumed Rs {s['fx']:.0f}/USD){extra}")

def log_usage(con, day, llm, label):
    """Store this session's usage in the runs log (shown by `python run.py coverage`). Returns the summary."""
    s = summarize(llm)
    if s["calls"]:
        db.log(con, day, "usage", json.dumps({**s, "label": label}))
    return s
