"""LLM abstraction: swap providers without touching pipeline code."""
import json, os, re, sys, time
from typing import Type, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

class CallBudgetExceeded(Exception): pass

class BaseLLM:
    provider = "base"
    def __init__(self, cap=None):
        self.calls, self.cap = 0, cap
        self.usage = {}                                  # model -> [input tokens, output tokens]
    def record(self, model, tin, tout):
        u = self.usage.setdefault(model, [0, 0]); u[0] += int(tin or 0); u[1] += int(tout or 0)
    def _tick(self):
        self.calls += 1
        if self.cap and self.calls > self.cap:
            raise CallBudgetExceeded(f"LLM call cap {self.cap} reached")
    def structured(self, prompt: str, schema: Type[T], tier="strong", system="") -> T:
        raise NotImplementedError

class AnthropicLLM(BaseLLM):
    provider = "anthropic"
    def __init__(self, cap=None):
        super().__init__(cap)
        import anthropic  # needs ANTHROPIC_API_KEY in the environment
        self.client = anthropic.Anthropic()
        self.models = {
            "cheap": os.getenv("GK_CHEAP_MODEL", "claude-haiku-4-5-20251001"),
            "strong": os.getenv("GK_STRONG_MODEL", "claude-sonnet-5-5"),
        }
    def structured(self, prompt, schema, tier="strong", system=""):
        sys = (system + "\nReturn ONLY valid JSON matching this schema. No prose, no code fences:\n"
               + json.dumps(schema.model_json_schema()))
        last = None
        for _ in range(2):
            self._tick()
            r = self.client.messages.create(model=self.models[tier], max_tokens=2500, system=sys,
                                            messages=[{"role": "user", "content": prompt}])
            u = getattr(r, "usage", None)
            self.record(self.models[tier], getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0))
            text = "".join(b.text for b in r.content if b.type == "text").strip()
            text = re.sub(r"^```(?:json)?|```$", "", text).strip()
            try:
                return schema.model_validate_json(text)
            except Exception as e:  # one corrective retry
                last = e
                prompt += f"\n\nYour previous output was invalid ({e}). Return corrected JSON only."
        raise last

def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]

class MockLLM(BaseLLM):
    provider = "mock"
    @staticmethod
    def _note_kwargs(title, art):
        art = re.sub(r"(?m)^SOURCE \d+ \(.*?\):\n", "", art)
        sents = _sentences(art)
        claims = [dict(text=x.rstrip("."), kind="number" if re.search(r"\d", x) else "other", source_sentence=x) for x in sents[:4]]
        return dict(headline=title, sector="Schemes", bullets=[x for x in sents[:5]], exam_angle="Mock exam angle", importance="High",
                    claims=claims, lens_points={"UPSC_PSC": "Mock UPSC angle", "SSC_BANKING": "Mock fact to memorise"})
    """Deterministic offline stand-in so the pipeline and tests run without an API key."""
    def structured(self, prompt, schema, tier="strong", system=""):
        self._tick()
        name = schema.__name__
        title = re.search(r"TITLE: (.*)", prompt)
        title = title.group(1).strip() if title else ""
        art = prompt.split("ARTICLE:\n", 1)[1].strip() if "ARTICLE:\n" in prompt else ""
        if name == "RelevanceBatch":
            out = []
            for m in re.finditer(r"^\[(\d+)\] \(tier \d[^)]*\) (.*?) :: (.*)$", prompt, re.M):
                low = (m.group(2) + " " + m.group(3)).lower()
                junk = any(k in low for k in ["celebrity", "gossip", "murder"])
                out.append(dict(id=int(m.group(1)), keep=not junk, score=2 if junk else 8,
                                sector="Other" if junk else "Schemes", lenses=[] if junk else ["UPSC_PSC", "SSC_BANKING"]))
            return schema(items=out)
        if name == "Relevance":
            low = (title + " " + art).lower()
            junk = any(k in low for k in ["celebrity", "gossip", "murder"])
            return schema(keep=not junk, score=2 if junk else 8, sector="Other" if junk else "Schemes",
                          lenses=[] if junk else ["UPSC_PSC", "SSC_BANKING"], reason="mock")
        if name == "NoteBatch":
            out = [dict(event=int(m.group(1)), **self._note_kwargs(m.group(2).strip(), m.group(3).strip()))
                   for m in re.finditer(r"=== EVENT (\d+) ===\nTITLE: ([^\n]*)\nSOURCES:\n(.*?)(?==== EVENT |\Z)", prompt, re.S)]
            return schema(notes=out)
        if name == "Note":
            return schema(**self._note_kwargs(title, art))
        if name == "QuizSet":
            h = re.search(r"HEADLINE: (.*)", prompt).group(1)
            return schema(mcqs=[dict(question=f"Which of these relates to: {h}?",
                                     options=["A", "B", "C", "D"], answer_index=0, explanation="mock")])
        if name == "RateStatement":
            return schema(is_rate_announcement=False, announced_on=None, rates={})
        if name == "DraftRows":
            cols = [c.strip() for c in re.search(r"Columns: ([^\n]*?)\. ", prompt + ". ").group(1).split(",")]
            return schema(rows=[{c: f"Sample {self.calls}.{i} {c}" for c in cols} for i in range(1, 4)])
        if name == "EventGroups":
            return schema(groups=[])
        if name == "EditorialDraft":
            h = re.search(r"TOPIC OR ANGLE: (.*)", prompt).group(1)
            return schema(title=f"Editorial: {h}", body_md="Mock background paragraph.\n\nMock analysis paragraph.\n\n- point one\n- point two\n\nMock conclusion.")
        if name == "GDTopic":
            h = re.search(r"(?:HEADLINE|TOPIC): (.*)", prompt).group(1)
            return schema(topic=f"Debate: {h}", context="Mock context.", for_points=["F1", "F2", "F3", "F4"],
                          against_points=["A1", "A2", "A3", "A4"], key_facts=["Outlay is Rs 5000 crore"],
                          opening="Mock opening.", conclusion="Mock conclusion.")
        if name == "SolverBatch":
            ids = [int(i) for i in re.findall(r"^\[(\d+)\] ", prompt, re.M)]
            return schema(answers=[dict(id=i, chosen_index=0, confident=True) for i in ids])
        if name == "SolverAnswer":
            return schema(chosen_index=0, confident=True)
        raise ValueError(name)


class QuotaExhausted(CallBudgetExceeded):
    """Provider's daily quota is used up. The run stops cleanly; progress is saved and the next run resumes."""

_RETRY_IN = re.compile(r"retry in ([\d.]+)s", re.I)
_TRANSIENT = re.compile(r"\b(500|502|503|504)\b|UNAVAILABLE|INTERNAL|DEADLINE_EXCEEDED|high demand|overloaded", re.I)

class GeminiLLM(BaseLLM):
    """Google Gemini via the google-genai SDK. Reads GEMINI_API_KEY (or GOOGLE_API_KEY).
    Free tiers allow few requests per minute, so calls are throttled per model (GK_GEMINI_RPM, default 10)
    and 429 errors wait for the delay Google asks for. Model names change often: set
    GK_GEMINI_CHEAP_MODEL / GK_GEMINI_STRONG_MODEL and run `python run.py check-llm`."""
    provider = "gemini"

    def __init__(self, cap=None, client=None, sleep=time.sleep, rpm=None, clock=time.monotonic):
        super().__init__(cap)
        self._sleep, self._clock = sleep, clock
        self.rpm = rpm or int(os.getenv("GK_GEMINI_RPM", "10"))
        self.fallback = os.getenv("GK_GEMINI_FALLBACK", "1") != "0"     # strong model out of daily quota -> use the cheap model's quota
        self.fell_back = False
        self._last = {}
        if client is None:
            from google import genai
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
        self.client = client
        self.models = {
            "cheap": os.getenv("GK_GEMINI_CHEAP_MODEL", "gemini-3.5-flash-lite"),
            "strong": os.getenv("GK_GEMINI_STRONG_MODEL", "gemini-3.5-flash"),
        }

    def _throttle(self, model):
        gap, now = 60.0 / self.rpm, self._clock()
        last = self._last.get(model)
        if last is not None and now - last < gap:
            self._sleep(gap - (now - last))
            now = self._clock()
        self._last[model] = now

    def structured(self, prompt, schema, tier="strong", system=""):
        sys_prompt = (system + "\nReturn ONLY valid JSON matching this schema. No prose, no code fences:\n"
                      + json.dumps(schema.model_json_schema()))
        config = {"system_instruction": sys_prompt, "response_mime_type": "application/json",
                  "max_output_tokens": 4096}
        model = self.models[tier]
        rate_waits = errors = bad_json = busy = 0
        while True:
            self._tick()
            self._throttle(model)
            try:
                r = self.client.models.generate_content(model=model, contents=prompt, config=config)
            except Exception as e:
                msg = str(e)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    self.calls -= 1                      # rate-limited calls are not billed: don't spend the cap
                    if "limit: 0" in msg:
                        raise RuntimeError(f"Model {model} has no free-tier quota on your plan; choose another model") from e
                    if "PerDay" in msg:
                        if tier == "strong" and self.fallback and self.models["strong"] != self.models["cheap"]:
                            print(f"  {model} daily quota used up - continuing with {self.models['cheap']}", flush=True)
                            self.models["strong"] = self.models["cheap"]; self.fell_back = True; model = self.models["strong"]
                            continue
                        raise QuotaExhausted(f"Gemini daily quota used up for {model} - progress is saved, run again after it resets") from e
                    rate_waits += 1
                    if rate_waits > 6:
                        raise
                    m = _RETRY_IN.search(msg)
                    wait = float(m.group(1)) + 1 if m else 15 * rate_waits
                    print(f"  rate limited by Gemini - waiting {wait:.0f}s", flush=True)
                    self._sleep(wait)
                    continue
                if _TRANSIENT.search(msg):                # Google is overloaded (503 etc.): wait longer, don't spend the cap
                    self.calls -= 1
                    busy += 1
                    if busy > 5: raise
                    wait = min(15 * 2 ** (busy - 1), 90)
                    print(f"  Gemini is busy - waiting {wait}s", flush=True)
                    self._sleep(wait); continue
                errors += 1                               # other error
                if errors >= 3:
                    raise
                self._sleep(5 * errors)
                continue
            u = getattr(r, "usage_metadata", None)     # thinking tokens are billed as output
            self.record(model, getattr(u, "prompt_token_count", 0), (getattr(u, "candidates_token_count", 0) or 0) + (getattr(u, "thoughts_token_count", 0) or 0))
            text = re.sub(r"^```(?:json)?|```$", "", (r.text or "").strip()).strip()
            try:
                return schema.model_validate_json(text)
            except Exception as e:
                bad_json += 1
                if bad_json >= 2:
                    raise
                prompt += f"\n\nYour previous output was invalid ({e}). Return corrected JSON only."

def choose_provider(env=None):
    """GK_PROVIDER = anthropic | gemini | mock | auto (default). Auto prefers Anthropic if both keys exist."""
    env = os.environ if env is None else env
    want = (env.get("GK_PROVIDER") or "auto").lower()
    has_a = bool(env.get("ANTHROPIC_API_KEY"))
    has_g = bool(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"))
    if want in ("anthropic", "claude"):
        if not has_a: raise RuntimeError("GK_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        return "anthropic"
    if want == "gemini":
        if not has_g: raise RuntimeError("GK_PROVIDER=gemini but GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        return "gemini"
    if want == "mock":
        return "mock"
    if has_a: return "anthropic"
    if has_g: return "gemini"
    return "mock"

def get_llm(mock=False, cap=None):
    p = "mock" if mock else choose_provider()
    if p == "mock":
        if not mock:
            print("WARNING: no API key found - using the MOCK model. Output is not real.", file=sys.stderr)
        return MockLLM(cap)
    return AnthropicLLM(cap) if p == "anthropic" else GeminiLLM(cap)
