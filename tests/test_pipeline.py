import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from gk import db, pipeline, aggregate, config, verify
from gk.schemas import Claim

ROOT = config.ROOT
DEMO = str(ROOT / "data/demo/sources_demo.yaml")

def fresh(tmp_path):
    os.chdir(ROOT)
    config.OUT_DIR = tmp_path
    return db.connect(str(tmp_path / "t.db"))

def test_daily_demo(tmp_path):
    con = fresh(tmp_path)
    s = pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=con)
    assert s["fetched"] == 2                 # gossip item filtered by avoid-list before storage
    assert s["notes"] == 1                   # two articles on one event -> one note
    assert s["routes"] == {"auto": 1}        # tier-1 source + all claims grounded
    n = con.execute("SELECT * FROM notes").fetchone()
    assert n["status"] == "pending"          # shadow mode: human approval required
    assert con.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1

def test_idempotent(tmp_path):
    con = fresh(tmp_path)
    pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=con)
    s2 = pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=con)
    assert s2["fetched"] == 0 and s2["notes"] == 0
    assert con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1

def test_weekly_needs_approval(tmp_path):
    con = fresh(tmp_path)
    pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=con)
    assert "0 approved notes" in aggregate.weekly(con, "2026-10-05").read_text()
    con.execute("UPDATE notes SET status='approved'"); con.execute("UPDATE questions SET status='approved'"); con.commit()
    md = aggregate.weekly(con, "2026-10-05").read_text()
    assert "1 approved notes" in md and "Rs 5000 crore" in md and "Practice questions" in md

def test_digit_slip_is_caught():
    src = "The scheme has an outlay of Rs 5000 crore."
    good = Claim(text="Outlay is Rs 5000 crore", kind="number", source_sentence=src)
    bad = Claim(text="Outlay is Rs 6000 crore", kind="number", source_sentence=src)
    fake = Claim(text="Outlay is Rs 5000 crore", kind="number", source_sentence="Totally different sentence.")
    assert verify.claim_grounded(good, src)
    assert not verify.claim_grounded(bad, src)
    assert not verify.claim_grounded(fake, src)

# ---------- editorial, lenses, gold ----------
from gk import editorial, gold
from gk.llm import MockLLM
from gk.schemas import Note

def _published_demo(tmp_path):
    con = fresh(tmp_path)
    pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=con)
    con.execute("UPDATE notes SET status='approved'"); con.execute("UPDATE questions SET status='approved'"); con.commit()
    return con

def test_editorial_in_digests_and_draft_excluded(tmp_path):
    con = _published_demo(tmp_path)
    editorial.add(con, "Why rural credit matters", "Admin analysis here.", "2026-10-05", status="published")
    editorial.add(con, "Secret draft", "Not ready.", "2026-10-05", status="draft")
    for md in (aggregate.daily(con, "2026-10-05").read_text(), aggregate.weekly(con, "2026-10-05").read_text(),
               aggregate.monthly(con, 2026, 10).read_text()):
        assert "## Editorial" in md and "Why rural credit matters" in md and "Admin analysis here." in md
        assert "Secret draft" not in md
    ed_id = con.execute("SELECT id FROM editorials WHERE title='Secret draft'").fetchone()[0]
    editorial.set_status(con, ed_id, "published")
    assert "Secret draft" in aggregate.daily(con, "2026-10-05").read_text()

def test_editorial_lens_targeting(tmp_path):
    con = _published_demo(tmp_path)
    editorial.add(con, "Banking only piece", "x", "2026-10-05", ["SSC_BANKING"], status="published")
    editorial.add(con, "For everyone", "y", "2026-10-05", [], status="published")
    ssc = aggregate.daily(con, "2026-10-05", "SSC_BANKING").read_text()
    defence = aggregate.daily(con, "2026-10-05", "DEFENCE").read_text()
    assert "Banking only piece" in ssc and "For everyone" in ssc
    assert "Banking only piece" not in defence and "For everyone" in defence

def test_lens_views_differ_in_style(tmp_path):
    con = _published_demo(tmp_path)
    upsc = aggregate.weekly(con, "2026-10-05", "UPSC_PSC").read_text()
    ssc = aggregate.weekly(con, "2026-10-05", "SSC_BANKING").read_text()
    mgmt = aggregate.weekly(con, "2026-10-05", "MANAGEMENT").read_text()
    assert "Exam angle" in upsc and "Mock UPSC angle" in upsc
    assert "Remember:" in ssc and "Mock fact to memorise" in ssc and "Exam angle" not in ssc
    assert "Discussion point" not in mgmt or "Mock" not in mgmt   # no management lens_point in mock -> no invented one
    assert "0 approved notes" in aggregate.weekly(con, "2026-10-05", "DEFENCE").read_text()  # sector not relevant

def test_gold_passes_with_mock():
    rep = gold.run_gold(MockLLM())
    assert rep["n"] >= 3 and rep["passed"], rep

class SloppyLLM(MockLLM):
    """Simulates a regression: changes a number in the note."""
    def structured(self, prompt, schema, tier="strong", system=""):
        out = super().structured(prompt, schema, tier, system)
        if schema is Note:
            out.bullets = [b.replace("5000", "6000").replace("45", "54") for b in out.bullets]
        return out

def test_gold_catches_regression():
    rep = gold.run_gold(SloppyLLM())
    assert not rep["passed"]
    assert any(r["invented_numbers"] for r in rep["results"])

def test_decimals_are_grounded_and_slips_caught():
    src = "India scored 61.2 points, up from 58.9 points last year."
    assert verify.claim_grounded(Claim(text="India scored 61.2 points", kind="number", source_sentence=src), src)
    assert not verify.claim_grounded(Claim(text="India scored 61.8 points", kind="number", source_sentence=src), src)

# ---------- Gemini provider ----------
from types import SimpleNamespace
from gk.llm import GeminiLLM, choose_provider, CallBudgetExceeded
from gk.schemas import SolverAnswer

class _Models:
    def __init__(self, outputs): self.outputs, self.calls = list(outputs), []
    def generate_content(self, model, contents, config=None):
        self.calls.append((model, contents, config))
        o = self.outputs.pop(0)
        if isinstance(o, Exception): raise o
        return SimpleNamespace(text=o)

def _gem(outputs, cap=None):
    m = _Models(outputs)
    return GeminiLLM(cap=cap, client=SimpleNamespace(models=m), sleep=lambda s: None), m

def test_gemini_parses_fenced_json_and_routes_models(monkeypatch):
    monkeypatch.setenv("GK_GEMINI_CHEAP_MODEL", "cheap-x"); monkeypatch.setenv("GK_GEMINI_STRONG_MODEL", "strong-x")
    g, m = _gem(['```json\n{"chosen_index": 1, "confident": true}\n```', '{"chosen_index": 0, "confident": false}'])
    g = GeminiLLM(client=SimpleNamespace(models=m), sleep=lambda s: None)
    assert g.structured("q", SolverAnswer, tier="cheap").chosen_index == 1
    assert g.structured("q", SolverAnswer, tier="strong").confident is False
    assert [c[0] for c in m.calls] == ["cheap-x", "strong-x"]
    assert m.calls[0][2]["response_mime_type"] == "application/json"

def test_gemini_corrective_retry_and_transient_error_retry():
    g, m = _gem(["not json at all", '{"chosen_index": 2, "confident": true}'])
    assert g.structured("q", SolverAnswer).chosen_index == 2
    assert "previous output was invalid" in m.calls[1][1]          # corrective prompt was sent
    g2, m2 = _gem([RuntimeError("429 rate limited"), '{"chosen_index": 0, "confident": true}'])
    assert g2.structured("q", SolverAnswer).confident is True and len(m2.calls) == 2

def test_gemini_call_cap_counts_every_attempt():
    g, _ = _gem([RuntimeError("x"), RuntimeError("x"), RuntimeError("x")], cap=2)
    try:
        g.structured("q", SolverAnswer); assert False
    except CallBudgetExceeded:
        pass

def test_provider_choice():
    assert choose_provider({}) == "mock"
    assert choose_provider({"GEMINI_API_KEY": "k"}) == "gemini"
    assert choose_provider({"GOOGLE_API_KEY": "k"}) == "gemini"
    assert choose_provider({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"}) == "anthropic"
    assert choose_provider({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g", "GK_PROVIDER": "gemini"}) == "gemini"
    for bad in ({"GK_PROVIDER": "gemini"}, {"GK_PROVIDER": "anthropic"}):
        try:
            choose_provider(bad); assert False
        except RuntimeError:
            pass

# ---------- rate limits, batching, resilience ----------
from gk.llm import QuotaExhausted
from gk import analyze, config as _cfg

def _clock_gem(outputs, rpm=6):
    t = {"now": 0.0, "sleeps": []}
    def sleep(x): t["sleeps"].append(x); t["now"] += x
    m = _Models(outputs)
    g = GeminiLLM(client=SimpleNamespace(models=m), sleep=sleep, rpm=rpm, clock=lambda: t["now"])
    return g, m, t

OK = '{"chosen_index": 0, "confident": true}'

def test_gemini_throttles_per_model():
    g, m, t = _clock_gem([OK, OK, OK], rpm=6)             # 6 per minute -> 10s gap per model
    g.structured("q", SolverAnswer, tier="cheap")
    g.structured("q", SolverAnswer, tier="strong")        # different model: no wait
    assert t["sleeps"] == []
    g.structured("q", SolverAnswer, tier="cheap")         # same model again: must wait ~10s
    assert len(t["sleeps"]) == 1 and abs(t["sleeps"][0] - 10) < 0.01

def test_gemini_honours_retry_delay_and_does_not_spend_cap():
    msg = "429 RESOURCE_EXHAUSTED. Quota exceeded for metric ...PerMinutePerProjectPerModel-FreeTier. Please retry in 8.13s."
    g, m, t = _clock_gem([RuntimeError(msg), RuntimeError(msg), OK], rpm=6000)
    g.cap = 1                                             # only ONE billable call allowed
    assert g.structured("q", SolverAnswer).confident is True
    assert any(abs(x - 9.13) < 0.01 for x in t["sleeps"]) and g.calls == 1

def test_gemini_daily_quota_and_zero_limit_stop_cleanly():
    g, _, _ = _clock_gem([RuntimeError("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")], rpm=6000)
    try:
        g.structured("q", SolverAnswer, tier="cheap"); assert False
    except QuotaExhausted:
        pass
    g2, _, _ = _clock_gem([RuntimeError("429 RESOURCE_EXHAUSTED limit: 0, model: x")], rpm=6000)
    try:
        g2.structured("q", SolverAnswer); assert False
    except RuntimeError as e:
        assert "no free-tier quota" in str(e)

def _add_articles(con, n, tier=2):
    for i in range(n):
        con.execute("INSERT INTO articles(url,source,tier,title,text,fetched_on) VALUES(?,?,?,?,?,?)",
                    (f"https://x/{i}", "s", tier, f"Unique headline number {i} alpha{i} beta{i}", "Sentence one. Sentence two. Sentence three. Sentence four.", "2026-10-05"))
    con.commit()

def test_relevance_is_batched(tmp_path, monkeypatch):
    con = fresh(tmp_path); monkeypatch.setattr(_cfg, "RELEVANCE_BATCH", 10)
    _add_articles(con, 25)
    llm = MockLLM()
    analyze.score_articles(con, llm, "2026-10-05", {"lenses": []})
    assert llm.calls == 3                                  # 25 articles -> 3 calls, not 25
    assert con.execute("SELECT COUNT(*) FROM articles WHERE status='new'").fetchone()[0] == 0

def test_failed_batch_does_not_crash_and_resumes(tmp_path, monkeypatch):
    con = fresh(tmp_path); monkeypatch.setattr(_cfg, "RELEVANCE_BATCH", 10)
    _add_articles(con, 12)
    class Flaky(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema.__name__ == "RelevanceBatch" and self.calls == 0:
                self.calls += 1; raise ValueError("bad json")
            return super().structured(prompt, schema, tier, system)
    analyze.score_articles(con, Flaky(), "2026-10-05", {"lenses": []})
    assert con.execute("SELECT COUNT(*) FROM articles WHERE status='new'").fetchone()[0] == 10   # first batch retried later
    assert con.execute("SELECT COUNT(*) FROM runs WHERE stage='score_error'").fetchone()[0] == 1
    analyze.score_articles(con, MockLLM(), "2026-10-06", {"lenses": []})                         # next run resumes
    assert con.execute("SELECT COUNT(*) FROM articles WHERE status='new'").fetchone()[0] == 0

def test_quota_stop_mid_run_keeps_and_verifies_finished_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "NOTE_BATCH", 1)
    con = fresh(tmp_path)
    _add_articles(con, 2, tier=1)
    class QuotaAfterOne(MockLLM):
        notes = 0
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is Note:
                QuotaAfterOne.notes += 1
                if QuotaAfterOne.notes > 1: raise QuotaExhausted("daily quota used up")
            return super().structured(prompt, schema, tier, system)
    s = pipeline.run_daily("2026-10-05", sources_path=DEMO, con=con, llm=QuotaAfterOne())
    assert "stopped" in s and s["notes"] == 1
    assert con.execute("SELECT route FROM notes").fetchone()[0] is not None         # finished note was still verified
    assert con.execute("SELECT COUNT(*) FROM articles WHERE status='kept'").fetchone()[0] >= 2   # rest waits for next run

# ---------- reader site ----------
def test_build_site_uses_only_approved_content(tmp_path):
    from gk.site import build_site
    con = _published_demo(tmp_path)
    editorial.add(con, "Pub ed", "**Bold** text", "2026-10-05", status="published")
    editorial.add(con, "Draft ed", "hidden", "2026-10-05", status="draft")
    idx, n, q, e = build_site(con, tmp_path / "site")
    js = (tmp_path / "site" / "data.js").read_text(encoding="utf-8")
    assert idx.exists() and "GK_DATA" in idx.read_text(encoding="utf-8") + js
    assert (n, q, e) == (1, 1, 1) and "Pub ed" in js and "Draft ed" not in js and "Rs 5000 crore" in js
    con.execute("UPDATE notes SET status='rejected'"); con.commit()
    assert build_site(con, tmp_path / "site2")[1] == 0       # rejected notes never reach the site

# ---------- local admin app (no Streamlit) ----------
import threading, urllib.request, urllib.error
from gk.admin import make_server

def _call(port, path, body=None, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **({"Host": host} if host else {})})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read() or b"{}") if r.headers["Content-Type"].startswith("application/json") else r.read()
    except urllib.error.HTTPError as e:
        return e.code, {}

def test_admin_app_end_to_end(tmp_path):
    con = fresh(tmp_path)
    pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=con)
    srv = make_server(0, db_path=str(tmp_path / "t.db"), site_dir=tmp_path / "site")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _call(port, "/")[0] == 200
        notes = _call(port, "/api/notes?status=pending&route=all")[1]
        assert len(notes) == 1 and notes[0]["claims"]
        n = notes[0]
        assert _call(port, "/api/note", dict(id=n["id"], action="approve", headline="Edited headline", bullets=["A", "B", "C"], exam_angle="x"))[1]["ok"]
        assert _call(port, "/api/editorial", dict(action="save", id=0, title="Ed", body_md="Body", day="2026-10-05", lenses=[], status="published"))[1]["ok"]
        assert '"title": "Ed"' in (tmp_path / "site" / "data.js").read_text(encoding="utf-8")      # auto-rebuilt, no manual build
        assert _call(port, "/api/editorial", dict(action="save", id=0, title="", body_md="", day="2026-10-05", lenses=[], status="draft"))[1]["error"]
        r = _call(port, "/api/build", {})[1]
        assert (r["notes"], r["editorials"]) == (1, 1) and "Edited headline" in (tmp_path / "site" / "data.js").read_text(encoding="utf-8")
        assert _call(port, "/site/index.html")[0] == 200 and _call(port, "/site/../t.db")[0] == 404
        assert _call(port, "/api/meta", host="evil.example.com")[0] == 403          # DNS-rebinding guard
    finally:
        srv.shutdown()

# ---------- sectors ----------
from gk.sectors import canon, repair

def test_canon_maps_free_text_and_headlines():
    assert canon("Polity & Governance") == "Polity" and canon("International Relations") == "IR"
    assert canon("Science and Technology") == "S&T" and canon("Reports & Indices") == "Reports&Indices"
    assert canon("Economy") == "Economy" and canon("") == "Other" and canon("???") == "Other"
    assert canon("Other", "US's Russia Sanctions Bill: 100% Tariffs on Russian Oil Buyers") == "IR"       # headline fallback
    assert canon("Other", "Key Takeaways from Supreme Court's Split Verdict on EC Appointment Law") == "Polity"

def test_new_notes_get_canonical_sector_and_repair_fixes_old_ones(tmp_path):
    con = fresh(tmp_path)
    class FreeText(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            out = super().structured(prompt, schema, tier, system)
            if schema is Note: out.sector = "Polity and Governance"       # what Gemini actually returned
            return out
    pipeline.run_daily("2026-10-05", sources_path=DEMO, con=con, llm=FreeText())
    assert con.execute("SELECT sector FROM notes").fetchone()[0] == "Polity"
    con.execute("UPDATE notes SET sector='Polity and Governance'"); con.commit()      # simulate the old broken data
    assert repair(con) == {"Polity": 1} and con.execute("SELECT sector FROM notes").fetchone()[0] == "Polity"

# ---------- balanced coverage ----------
from gk.sectors import balanced_pick
from gk import coverage

def test_balanced_pick_gives_every_sector_a_slot():
    items = [(1, 10, "Polity"), (2, 10, "Polity"), (3, 9, "Polity"), (4, 7, "Awards"), (5, 6, "Economy")]
    assert set(balanced_pick(items, 3)) == {2, 4, 5}          # not three Polity stories
    assert len(balanced_pick(items, 10)) == 5

def test_second_run_still_writes_new_notes_and_spreads_sectors(tmp_path, monkeypatch):
    con = fresh(tmp_path); monkeypatch.setattr(_cfg, "MAX_NOTES_PER_DAY", 2)
    for cid, score, sec in [(1, 10, "Polity"), (2, 9, "Polity"), (3, 7, "Awards"), (4, 6, "Economy")]:
        con.execute("INSERT INTO articles(url,source,tier,title,text,fetched_on,cluster_id,status,rel_score,sector) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"u{cid}", "s", 1, f"Headline {cid}", "One. Two. Three. Four.", "d", cid, "kept", score, sec))
    con.execute("INSERT INTO notes(cluster_id,day,sector,headline,body_json,importance) VALUES(1,'d','Polity','h','{}','High')")
    con.commit()
    made, _ = analyze.build_notes(con, MockLLM(), "2026-10-05", {})
    assert len(made) == 2                                          # cluster 1 already had a note and no longer eats the limit
    assert [r[0] for r in con.execute("SELECT cluster_id FROM notes ORDER BY id")] == [1, 2, 3]

def test_coverage_report_finds_story_and_status(tmp_path):
    con = fresh(tmp_path); _add_articles(con, 3)
    out = coverage.report(con, "headline number 1")
    assert "ARTICLES BY SOURCE" in out and "[new/None/None]" in out and "not scored yet" in out

# ---------- fewer API calls per note ----------
from gk import quiz as _quiz
from gk.schemas import SolverBatch

def test_quiz_uses_two_calls_and_flags_solver_disagreement(tmp_path):
    con = fresh(tmp_path)
    note = MockLLM().structured("TITLE: T\nARTICLE:\nOne fact here. Two fact there. Three. Four.", Note)
    con.execute("INSERT INTO notes(cluster_id,day,sector,headline,body_json,importance) VALUES(1,'d','Polity','h','{}','High')"); con.commit()
    class Disagree(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            out = super().structured(prompt, schema, tier, system)
            if schema is SolverBatch:
                for a in out.answers: a.chosen_index = 3          # blind solver picks a different option
            return out
    llm = Disagree()
    _quiz.make_quiz(con, llm, 1, note)
    assert llm.calls == 2                                          # 1 write + 1 batched check (was 1 + one per question)
    q = con.execute("SELECT valid, solver_ok FROM questions").fetchone()
    assert (q["valid"], q["solver_ok"]) == (1, 0)                  # kept, but flagged for the admin

# ---------- freshness ----------
from gk import fetch

def test_old_articles_are_not_fetched_or_kept(tmp_path):
    con = fresh(tmp_path)
    (tmp_path / "f.xml").write_text('''<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Fresh story</title><link>https://x/fresh</link><pubDate>Mon, 05 Oct 2026 06:00:00 GMT</pubDate><description>Fresh.</description></item>
<item><title>Old story</title><link>https://x/old</link><pubDate>Mon, 21 Sep 2026 06:00:00 GMT</pubDate><description>Old.</description></item>
</channel></rss>''')
    cfg = {"sources": [{"id": "s", "tier": 1, "rss": str(tmp_path / "f.xml"), "enabled": True}], "avoid_keywords": []}
    assert fetch.fetch_all(con, "2026-10-05", cfg) == 1                       # the 14-day-old item is skipped
    con.execute("INSERT INTO articles(url,source,tier,title,published,text,fetched_on,status) VALUES('o1','s',1,'Old kept','Mon, 21 Sep 2026 06:00:00 GMT','t','d','kept')")
    con.execute("INSERT INTO articles(url,source,tier,title,published,text,fetched_on,status) VALUES('o2','s',1,'Recent kept','Sun, 04 Oct 2026 06:00:00 GMT','t','d','kept')")
    con.commit()
    assert fetch.drop_stale(con, "2026-10-05") == 1                           # stored old article is cleaned up
    assert con.execute("SELECT status FROM articles WHERE url='o1'").fetchone()[0] == "dropped"
    assert con.execute("SELECT status FROM articles WHERE url='o2'").fetchone()[0] == "kept"

# ---------- business section + GD topics ----------
from gk import gd
from gk.site import GROUPS, build_site

def test_business_is_its_own_section():
    assert GROUPS["Business & Corporate"] == ["Business"] and "Business" not in GROUPS["Economy"]

def test_gd_generation_drops_invented_numbers_and_never_duplicates(tmp_path):
    con = _published_demo(tmp_path)
    class Invented(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            out = super().structured(prompt, schema, tier, system)
            if schema.__name__ == "GDTopic": out.key_facts = ["Outlay is Rs 5000 crore", "Cost is Rs 9999 crore"]
            return out
    assert gd.generate(con, Invented(), "2026-10-05") == 1
    row = con.execute("SELECT * FROM gd_topics").fetchone()
    assert row["status"] == "pending" and json.loads(row["facts_json"]) == ["Outlay is Rs 5000 crore"]
    assert gd.generate(con, Invented(), "2026-10-05") == 0                         # same note is not turned into a second topic

def test_gd_admin_review_and_site(tmp_path):
    con = _published_demo(tmp_path); gd.generate(con, MockLLM(), "2026-10-05")
    srv = make_server(0, db_path=str(tmp_path / "t.db"), site_dir=tmp_path / "site"); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        g = _call(port, "/api/gd?status=pending")[1]
        assert len(g) == 1 and len(g[0]["for_points"]) == 4
        assert "gd" in json.loads((tmp_path / "site" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1]) if (tmp_path / "site" / "data.js").exists() else True
        body = dict(id=g[0]["id"], action="approve", topic="Edited topic", context="c", for_points=["a"], against_points=["b"], key_facts=[], opening="o", conclusion="z")
        assert _call(port, "/api/gd", body)[1]["ok"]
        data = json.loads((tmp_path / "site" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
        assert [t["topic"] for t in data["gd"]] == ["Edited topic"]               # approved topic reached the site automatically
        assert _call(port, "/api/gd", dict(action="create", topic="", context=""))[1]["error"]
        assert _call(port, "/api/gd", dict(action="create", topic="My own topic", context="c", for_points=["x"], against_points=["y"]))[1]["ok"]
        assert len(_call(port, "/api/gd?status=approved")[1]) == 2
    finally:
        srv.shutdown()

# ---------- user accounts ----------
import http.client
from gk import users
from gk.serve import make_public_server

def _pub(tmp_path, verifier=None, monkeypatch=None):
    fresh(tmp_path); (tmp_path / "site").mkdir(exist_ok=True); (tmp_path / "site" / "index.html").write_text("<html>home</html>")
    (tmp_path / "secret.db").write_text("x")
    kw = {"verifier": verifier} if verifier else {}
    srv = make_public_server(0, db_path=str(tmp_path / "t.db"), site_dir=tmp_path / "site", **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]

def _r(port, method, path, body=None, cookie=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port)
    h = {"Content-Type": "application/json", **(headers or {})}
    if cookie: h["Cookie"] = cookie
    c.request(method, path, None if body is None else json.dumps(body), h)
    r = c.getresponse(); raw = r.read(); sc = r.getheader("Set-Cookie")
    try: data = json.loads(raw)
    except Exception: data = raw
    return r.status, data, (sc.split(";")[0] if sc else None), sc

GOOD = dict(name="Asha", email="Asha@Example.com", password="correct horse", age_band="18-24", exams=["UPSC_PSC", "bogus"], consent=True)

def test_password_hash_and_signup_validation(tmp_path):
    h = users.hash_pw("secret123")
    assert "secret123" not in h and users.check_pw("secret123", h) and not users.check_pw("wrong", h)
    con = fresh(tmp_path)
    for bad, msg in ((dict(GOOD, password="short"), "Password"), (dict(GOOD, email="nope"), "email"), (dict(GOOD, consent=False), "privacy"),
                     (dict(GOOD, age_band="12"), "age"), (dict(GOOD, name=""), "name")):
        assert msg.lower() in users.signup(con, bad)["error"].lower()
    assert "id" in users.signup(con, GOOD, {"UPSC_PSC"})
    assert "already" in users.signup(con, GOOD)["error"]
    assert con.execute("SELECT exams, consent_version, age_band FROM users").fetchone()[:] == ("UPSC_PSC", users.POLICY, "18-24")   # unknown exam dropped

def test_account_flow_over_http(tmp_path):
    srv, port = _pub(tmp_path)
    try:
        assert _r(port, "GET", "/auth/me")[1]["user"] is None
        st, d, ck, raw = _r(port, "POST", "/auth/signup", GOOD)
        assert st == 200 and d["user"]["email"] == "asha@example.com" and d["user"]["profile_complete"] and "HttpOnly" in raw and "SameSite=Lax" in raw
        assert _r(port, "GET", "/auth/me", cookie=ck)[1]["user"]["name"] == "Asha"
        assert _r(port, "POST", "/api/state", dict(bookmarks=[1, 2], hist=[{"k": 3, "t": 5}]), cookie=ck)[0] == 200
        assert _r(port, "GET", "/api/state", cookie=ck)[1]["bookmarks"] == [1, 2]          # follows the account across devices
        assert _r(port, "GET", "/api/state")[0] == 401                                       # not logged in
        assert _r(port, "POST", "/api/state", dict(bookmarks=["x"]), cookie=ck)[0] == 200
        assert _r(port, "GET", "/api/state", cookie=ck)[1]["bookmarks"] == [1, 2]            # junk input ignored
        ex = _r(port, "GET", "/auth/export", cookie=ck)[1]
        assert "pw_hash" not in ex["account"] and ex["saved_state"]["bookmarks"] == [1, 2]
        assert _r(port, "POST", "/auth/login", dict(email="asha@example.com", password="nope"))[0] == 400
        assert _r(port, "POST", "/auth/login", dict(email="ASHA@example.com", password="correct horse"))[0] == 200
        _r(port, "POST", "/auth/logout", {}, cookie=ck)
        assert _r(port, "GET", "/auth/me", cookie=ck)[1]["user"] is None                     # session really ended
        st, d, ck2, _ = _r(port, "POST", "/auth/login", dict(email="asha@example.com", password="correct horse"))
        assert _r(port, "POST", "/auth/delete", {"confirm": True}, cookie=ck2)[0] == 200
        assert tmp_path.joinpath("t.db").exists() and sqlite3_count(tmp_path) == (0, 0)      # account + state gone
    finally:
        srv.shutdown()

def sqlite3_count(tmp_path):
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "t.db"))
    return (c.execute("SELECT COUNT(*) FROM users").fetchone()[0], c.execute("SELECT COUNT(*) FROM user_state").fetchone()[0])

def test_login_lockout_and_no_user_enumeration(tmp_path):
    con = fresh(tmp_path); users._fails.clear(); users.signup(con, GOOD)
    msgs = {users.login(con, "asha@example.com", "bad")["error"] for _ in range(1)} | {users.login(con, "ghost@example.com", "bad")["error"]}
    assert len(msgs) == 1                                                                    # same message for unknown email
    for _ in range(5): users.login(con, "asha@example.com", "bad")
    assert "Too many" in users.login(con, "asha@example.com", "correct horse")["error"]      # locked even for the right password
    users._fails.clear()

def test_google_signin_with_injected_verifier(tmp_path, monkeypatch):
    monkeypatch.setenv("GK_GOOGLE_CLIENT_ID", "cid")
    def ver(cred, cid):
        if cred == "bad": raise ValueError("bad token")
        return dict(sub="g1", email="Kid@Gmail.com", email_verified=(cred != "unverified"), name="Kid")
    srv, port = _pub(tmp_path, verifier=ver)
    try:
        assert _r(port, "GET", "/auth/config")[1]["googleClientId"] == "cid"
        assert _r(port, "POST", "/auth/google", dict(credential="bad"))[0] == 400
        assert _r(port, "POST", "/auth/google", dict(credential="unverified"))[0] == 400
        st, d, ck, _ = _r(port, "POST", "/auth/google", dict(credential="ok"))
        assert st == 200 and d["user"]["email"] == "kid@gmail.com" and not d["user"]["profile_complete"]   # must still give age + consent
        assert _r(port, "POST", "/auth/profile", dict(age_band="Under 18", consent=False), cookie=ck)[0] == 400
        assert _r(port, "POST", "/auth/profile", dict(age_band="Under 18", consent=True), cookie=ck)[1]["user"]["profile_complete"]
        assert _r(port, "POST", "/auth/google", dict(credential="ok"))[1]["user"]["id"] == d["user"]["id"]  # same account on next login
    finally:
        srv.shutdown()

def test_server_rejects_cross_site_posts_and_path_tricks(tmp_path):
    srv, port = _pub(tmp_path)
    try:
        assert _r(port, "POST", "/auth/login", dict(email="a", password="b"), headers={"Origin": "http://evil.example"})[0] == 403
        assert _r(port, "POST", "/auth/login", dict(email="a", password="b"), headers={"Content-Type": "text/plain"})[0] == 403
        assert _r(port, "GET", "/")[1] == b"<html>home</html>"
        assert _r(port, "GET", "/../secret.db")[0] == 404 and _r(port, "GET", "/%2e%2e/secret.db")[0] == 404
    finally:
        srv.shutdown()

# ---------- admin-suggested GD topic ----------
def test_gd_from_admin_topic_uses_verified_context_and_blocks_invented_numbers(tmp_path):
    con = _published_demo(tmp_path)
    seen = {}
    class Cap(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            out = super().structured(prompt, schema, tier, system)
            if schema.__name__ == "GDTopic":
                seen["prompt"] = prompt; out.topic = "model rewrote it"
                out.key_facts = ["Outlay is Rs 5000 crore", "Farm output rose 42 percent"]       # second fact has an invented number
            return out
    gid = gd.generate_for_topic(con, Cap(), "Is the Demo Rural Credit Scheme a good idea for small farmers?", "Mention NABARD")
    row = con.execute("SELECT * FROM gd_topics WHERE id=?", (gid,)).fetchone()
    assert row["topic"] == "Is the Demo Rural Credit Scheme a good idea for small farmers?"       # editor's wording kept exactly
    assert row["status"] == "pending" and row["source"] == "admin-topic" and row["note_id"] is None
    assert json.loads(row["facts_json"]) == ["Outlay is Rs 5000 crore"]                           # came from the related approved note
    assert "Union Cabinet" in seen["prompt"] and "Mention NABARD" in seen["prompt"]
    for bad in ("", "abc", "x" * 300):
        try: gd.generate_for_topic(con, MockLLM(), bad); assert False
        except ValueError: pass

def test_gd_from_topic_api(tmp_path):
    con = _published_demo(tmp_path)
    srv = make_server(0, db_path=str(tmp_path / "t.db"), site_dir=tmp_path / "site", llm_factory=lambda: MockLLM()); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _call(port, "/api/gd/from-topic", dict(topic="no"))[0] == 400
        assert "id" in _call(port, "/api/gd/from-topic", dict(topic="Should coaching centres be regulated?", extra=""))[1]
        g = _call(port, "/api/gd?status=pending")[1]
        assert len(g) == 1 and g[0]["source"] == "admin-topic" and len(g[0]["against_points"]) == 4
    finally:
        srv.shutdown()

# ---------- AI-drafted editorials ----------
import sqlite3
from gk import db as _db

def test_editorial_draft_is_unpublished_and_flags_unverified_numbers(tmp_path):
    con = _published_demo(tmp_path)
    seen = {}
    class Ed(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            out = super().structured(prompt, schema, tier, system)
            if schema.__name__ == "EditorialDraft":
                seen["p"] = prompt
                out.body_md = "The outlay is Rs 5000 crore.\n\nCredit to 37 lakh farmers will rise by 12 percent."
            return out
    eid = editorial.draft_from_topic(con, Ed(), "Will the Demo Rural Credit Scheme help small farmers?", "Mention NABARD")
    row = con.execute("SELECT * FROM editorials WHERE id=?", (eid,)).fetchone()
    assert (row["status"], row["source"]) == ("draft", "ai-draft")
    assert row["flags"] == "12, 37"                                        # 5000 is in the approved note; 37 and 12 are not
    assert "Union Cabinet" in seen["p"] and "Mention NABARD" in seen["p"]
    assert build_site(con, tmp_path / "site")[3] == 0                      # drafts never reach the public site
    editorial.set_status(con, eid, "published")
    assert build_site(con, tmp_path / "site")[3] == 1
    for bad in ("", "abc", "x" * 300):
        try: editorial.draft_from_topic(con, MockLLM(), bad); assert False
        except ValueError: pass

def test_editorial_draft_api(tmp_path):
    con = _published_demo(tmp_path)
    srv = make_server(0, db_path=str(tmp_path / "t.db"), site_dir=tmp_path / "site", llm_factory=lambda: MockLLM()); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _call(port, "/api/editorial/draft", dict(topic="no"))[0] == 400
        assert "id" in _call(port, "/api/editorial/draft", dict(topic="Why coaching regulation matters", extra=""))[1]
        e = _call(port, "/api/editorials")[1]
        assert len(e) == 1 and e[0]["source"] == "ai-draft" and e[0]["status"] == "draft"
    finally:
        srv.shutdown()

def test_old_databases_are_upgraded(tmp_path):
    p = str(tmp_path / "old.db"); c = sqlite3.connect(p)
    c.execute("CREATE TABLE editorials(id INTEGER PRIMARY KEY, day TEXT, title TEXT, body_md TEXT, lenses TEXT DEFAULT '', author TEXT DEFAULT 'admin', status TEXT DEFAULT 'draft', created TEXT, updated TEXT)")
    c.execute("INSERT INTO editorials(day,title,body_md,status) VALUES('2026-10-08','Old one','body','published')"); c.commit(); c.close()
    con = _db.connect(p)
    row = con.execute("SELECT * FROM editorials").fetchone()
    assert row["title"] == "Old one" and row["source"] == "admin" and row["flags"] == ""   # existing editorial kept, new columns added

# ---------- duplicates ----------
from gk import cluster
from gk.schemas import EventGroups

def _art(con, title, day="2026-10-12", cid=None, status="new", text="One. Two. Three. Four.", source="s", tier=2, score=8):
    con.execute("INSERT INTO articles(url,source,tier,title,text,fetched_on,cluster_id,status,rel_score,sector) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"u/{title}/{day}/{source}", source, tier, title, text, day, cid, status, score, "Economy")); con.commit()
    return con.execute("SELECT id FROM articles ORDER BY id DESC LIMIT 1").fetchone()[0]

def _cl(con):
    return {r["title"]: r["cluster_id"] for r in con.execute("SELECT title, cluster_id FROM articles")}

def test_reworded_headlines_of_one_story_share_a_cluster_but_opposite_outcomes_do_not(tmp_path):
    con = fresh(tmp_path)
    T = ["Inflation May Surge to 6-7%, RBI May Hike Repo Rate by 100-150 bps",
         "RBI May Hike Repo Rate by 75-100 Bps Amid Persistent Inflation, Warns SBI Capital",
         "RBI may raise repo rate by 75-100 bps in current cycle: Report",
         "RBI keeps repo rate unchanged at 5.5%", "RBI hikes repo rate by 50 bps"]
    for t in T: _art(con, t)
    cluster.assign_clusters(con); c = _cl(con)
    assert c[T[0]] == c[T[1]] == c[T[2]]                       # the three RBI-hike stories from the screenshot become one event
    assert len({c[T[0]], c[T[3]], c[T[4]]}) == 3               # "unchanged" and "hikes" are different events

def test_old_stories_do_not_swallow_new_developments(tmp_path):
    con = fresh(tmp_path)
    t = "Government approves new national education policy framework for schools"
    _art(con, t, day="2026-10-01", cid=1); _art(con, t + " today", day="2026-10-12")
    cluster.assign_clusters(con)
    assert con.execute("SELECT cluster_id FROM articles WHERE fetched_on='2026-10-12'").fetchone()[0] != 1

def test_ai_merge_puts_repeats_into_the_already_covered_note(tmp_path):
    con = fresh(tmp_path)
    con.execute("INSERT INTO notes(cluster_id,day,sector,headline,body_json,importance) VALUES(1,'2026-10-11','Economy','RBI hike story','{}','High')")
    a1 = _art(con, "RBI hike old", day="2026-10-11", cid=1, status="kept")
    a2 = _art(con, "Repo rate rise likely says report", cid=2, status="kept")
    a3 = _art(con, "Central bank may lift rates", cid=3, status="kept"); a4 = _art(con, "Rate lift expected by RBI", cid=4, status="kept")
    class G(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            return EventGroups(groups=[[1, 2], [3, 4], [4], [99, 3]]) if schema is EventGroups else super().structured(prompt, schema, tier, system)
    assert analyze.merge_events(con, G(), "2026-10-12") == 2
    cl = {r["id"]: r["cluster_id"] for r in con.execute("SELECT id, cluster_id FROM articles")}
    assert cl[a2] == 1                                          # repeat of yesterday's story: joins the covered cluster, no second note
    assert cl[a3] == cl[a4] == 3                                # two waiting duplicates: one cluster
    assert all(w[0] != 2 for w in analyze.waiting_clusters(con) and [(c,) for c, _, _ in analyze.waiting_clusters(con)])

def test_note_uses_all_sources_and_names_them(tmp_path, monkeypatch):
    con = fresh(tmp_path); monkeypatch.setattr(_cfg, "MAX_NOTES_PER_DAY", 5); seen = {}
    _art(con, "RBI may hike repo rate by 75-100 bps report", cid=1, status="kept", text="ET says the hike may be 75-100 bps. Second.", source="et", tier=2)
    _art(con, "RBI may hike repo rate 100-150 bps SBI", cid=1, status="kept", text="SBI Capital sees 100-150 bps. Second.", source="sbi", tier=2)
    class C(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is Note: seen["p"] = prompt
            return super().structured(prompt, schema, tier, system)
    analyze.build_notes(con, C(), "2026-10-12", {})
    assert "SOURCE 1 (" in seen["p"] and "SOURCE 2 (" in seen["p"] and "75-100" in seen["p"] and "100-150" in seen["p"]

def test_coverage_shows_daily_trend_and_runs(tmp_path):
    con = fresh(tmp_path); _art(con, "Some story headline here today", status="kept")
    con.execute("INSERT INTO runs(day,stage,detail) VALUES('2026-10-12','daily_done',\"{'fetched': 40, 'kept': 12, 'notes': 3, 'stopped': 'Gemini daily quota used up'}\")"); con.commit()
    out = coverage.report(con)
    assert "BY DAY" in out and "2026-10-12" in out and "RECENT RUNS" in out and "quota used up" in out


# ---------- fewer requests: batched notes, busy-server backoff, model fallback ----------
from gk.schemas import NoteBatch

def _five_clusters(con):
    for cid in range(1, 6):
        con.execute("INSERT INTO articles(url,source,tier,title,text,fetched_on,cluster_id,status,rel_score,sector) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"u{cid}", "s", 1, f"Headline {cid}", f"Fact {cid} one. Fact {cid} two. Fact {cid} three. Fact {cid} four.", "d", cid, "kept", 10 - cid, "Polity"))
    con.commit()

def test_notes_are_written_three_per_request(tmp_path, monkeypatch):
    con = fresh(tmp_path); _five_clusters(con); monkeypatch.setattr(_cfg, "NOTE_BATCH", 3)
    llm = MockLLM()
    made, stopped = analyze.build_notes(con, llm, "2026-10-13", {})
    assert len(made) == 5 and stopped is None
    assert llm.calls == 2                                           # 5 notes in 2 requests (3 + 2) instead of 5
    heads = {r["headline"] for r in con.execute("SELECT headline FROM notes")}
    assert heads == {f"Headline {i}" for i in range(1, 6)}          # each note kept its own event's facts
    for _, note, arts in made:
        assert all(note.headline.split()[-1] in c.text for c in note.claims)

def test_event_missing_from_batch_reply_is_left_for_next_run(tmp_path, monkeypatch):
    con = fresh(tmp_path); _five_clusters(con); monkeypatch.setattr(_cfg, "NOTE_BATCH", 3)
    class Drops(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            out = super().structured(prompt, schema, tier, system)
            if schema is NoteBatch: out.notes = [n for n in out.notes if n.event != 2]
            return out
    made, _ = analyze.build_notes(con, Drops(), "2026-10-13", {})
    assert len(made) == 3 and con.execute("SELECT COUNT(*) FROM runs WHERE stage='note_missing'").fetchone()[0] == 2   # event 2 of each batch was dropped
    assert len(analyze.waiting_clusters(con)) == 2                  # the dropped events are still waiting, not lost

def test_gemini_waits_longer_when_google_is_busy_and_does_not_spend_cap():
    busy = "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand.'}}"
    g, m, t = _clock_gem([RuntimeError(busy), RuntimeError(busy), OK], rpm=6000)
    g.cap = 1
    assert g.structured("q", SolverAnswer).confident is True
    assert any(abs(x - 15) < 0.01 for x in t["sleeps"]) and any(abs(x - 30) < 0.01 for x in t["sleeps"]) and g.calls == 1

def test_gemini_falls_back_to_the_cheap_model_when_strong_quota_is_gone(monkeypatch):
    monkeypatch.setenv("GK_GEMINI_STRONG_MODEL", "S"); monkeypatch.setenv("GK_GEMINI_CHEAP_MODEL", "C")
    day_quota = "429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    g, m, t = _clock_gem([RuntimeError(day_quota), OK, OK], rpm=6000)
    assert g.structured("q", SolverAnswer, tier="strong").confident is True
    assert g.fell_back and [c[0] for c in m.calls] == ["S", "C"]    # retried on the cheap model
    assert g.structured("q", SolverAnswer, tier="strong").confident is True and m.calls[-1][0] == "C"
    g2, _, _ = _clock_gem([RuntimeError(day_quota), RuntimeError(day_quota)], rpm=6000)
    try:
        g2.structured("q", SolverAnswer, tier="strong"); assert False                       # cheap model also out: stop cleanly
    except QuotaExhausted:
        pass
    monkeypatch.setenv("GK_GEMINI_FALLBACK", "0")
    g3, _, _ = _clock_gem([RuntimeError(day_quota)], rpm=6000)
    try:
        g3.structured("q", SolverAnswer, tier="strong"); assert False
    except QuotaExhausted:
        pass

# ---------- token usage and cost ----------
from gk import usage

def test_gemini_records_tokens_including_thinking_tokens(monkeypatch):
    monkeypatch.setenv("GK_GEMINI_STRONG_MODEL", "S")
    class M:
        def generate_content(self, model, contents, config=None):
            return SimpleNamespace(text=OK, usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=200, thoughts_token_count=300))
    g = GeminiLLM(client=SimpleNamespace(models=M()), sleep=lambda s: None, rpm=6000)
    g.structured("q", SolverAnswer); g.structured("q", SolverAnswer)
    assert g.usage == {"S": [2000, 1000]}                       # output = answer + thinking tokens (billed as output)

def test_cost_estimate_uses_price_table_and_flags_unknown_models(monkeypatch):
    monkeypatch.setenv("GK_USD_INR", "100")
    llm = MockLLM(); llm.calls = 7
    llm.record("gemini-3.5-flash", 1_000_000, 100_000); llm.record("gemini-3.5-flash-lite", 2_000_000, 0); llm.record("mystery-model", 5, 5)
    s = usage.summarize(llm)
    assert abs(s["usd"] - (1.5 + 0.9 + 0.6)) < 0.001 and s["inr"] == round(s["usd"] * 100, 2) and s["unknown"] == ["mystery-model"]
    assert "7 calls" in usage.line(s) and "no price set for: mystery-model" in usage.line(s) and "Rs 300" in usage.line(s)

def test_usage_is_logged_and_shown_by_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv("GK_USD_INR", "100")
    con = fresh(tmp_path)
    for label, tin in (("daily", 1_000_000), ("gd", 500_000)):
        llm = MockLLM(); llm.calls = 3; llm.record("gemini-3.5-flash-lite", tin, 0)
        usage.log_usage(con, "2026-10-13", llm, label)
    usage.log_usage(con, "2026-10-13", MockLLM(), "idle")                 # zero calls: nothing logged
    out = coverage.report(con)
    assert "AI USAGE" in out and "2026-10" in out and "$0.45" in out and "Rs 45" in out   # 1.5M tokens in at $0.30/M
    assert con.execute("SELECT COUNT(*) FROM runs WHERE stage='usage'").fetchone()[0] == 2

def test_daily_run_reports_estimated_cost(tmp_path):
    con = fresh(tmp_path)
    s = pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=con)
    assert s["est_cost_usd"] == 0 and s["est_cost_inr"] == 0              # mock model: no tokens, no cost
    assert con.execute("SELECT COUNT(*) FROM runs WHERE stage='usage'").fetchone()[0] == 1

# ---------- Static GK ----------
from gk import static

def test_static_categories_exist_and_entries_are_validated(tmp_path):
    con = fresh(tmp_path)
    assert {"banking-terms", "countries", "states", "important-days", "books-authors", "constitution", "india-indices"} <= {c["slug"] for c in static.overview(con)}
    i = static.add_item(con, "countries", dict(country="India", capital="New Delhi", currency="Indian Rupee", continent="Asia"))
    for bad in (dict(country="", capital="x"), dict(country="India", capital="New Delhi", currency="Indian Rupee", continent="Asia")):
        try: static.add_item(con, "countries", bad); assert False
        except ValueError: pass
    static.add_item(con, "countries", dict(country="India", capital="Delhi"))              # same key but different row is allowed for tables
    static.add_item(con, "banking-terms", dict(term="Repo Rate", definition="d"))
    try: static.add_item(con, "banking-terms", dict(term="repo rate", definition="other")); assert False   # glossary terms are unique
    except ValueError: pass
    try: static.list_items(con, "no-such"); assert False
    except ValueError: pass

def test_changes_keep_history_and_volatile_entries_come_due_sooner(tmp_path):
    con = fresh(tmp_path)
    i = static.add_item(con, "union-cabinet", dict(ministry="Finance", minister="A", since="2024"), status="approved", as_on="2026-01-01")
    j = static.add_item(con, "countries", dict(country="X", capital="Y", currency="Z", continent="Q"), status="approved", as_on="2026-01-01")
    due = {d["id"] for d in static.due_for_review(con, "2026-03-01")}
    assert i in due and j not in due                                        # volatile: 30 days; stable: a year
    static.update_item(con, i, data=dict(ministry="Finance", minister="B", since="2026"), as_on="2026-03-01", note="reshuffle")
    h = static.history(con, i)
    assert len(h) == 2 and h[0]["old"]["data"]["minister"] == "A" and h[0]["new"]["data"]["minister"] == "B" and h[0]["note"] == "reshuffle"
    assert i not in {d["id"] for d in static.due_for_review(con, "2026-03-15")}   # re-checking resets the clock

def test_csv_import(tmp_path):
    con = fresh(tmp_path)
    r = static.import_csv(con, "countries", "wrong,header\n1,2")
    assert r["added"] == 0 and "must include 'country'" in r["errors"][0]
    csv_text = "Country,Capital,Currency,Continent,Extra\nFrance,Paris,Euro,Europe,ignored\nJapan,Tokyo,Yen,Asia,ignored\n,,,,\nFrance,Paris,Euro,Europe,ignored\n"
    r = static.import_csv(con, "countries", csv_text)
    assert (r["added"], r["skipped"]) == (2, 1) and r["ignored_columns"] == ["Extra"] and len(r["errors"]) == 1
    assert {i["status"] for i in static.list_items(con, "countries")} == {"pending"}
    assert static.import_csv(con, "countries", "country,capital\nItaly,Rome", approve=True)["added"] == 1
    assert static.list_items(con, "countries", "approved")[0]["data"]["capital"] == "Rome"

def test_starter_set_is_pending_idempotent_and_only_approved_entries_reach_site_and_quizzes(tmp_path):
    con = fresh(tmp_path)
    n = static.seed_starter(con)
    assert n > 60 and static.seed_starter(con) == 0
    assert all(i["status"] == "pending" for c in ("states", "banking-terms") for i in static.list_items(con, c))
    assert build_site(con, tmp_path / "s0")[0].exists()
    data = json.loads((tmp_path / "s0" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
    assert data["static"] == [] and data["static_questions"] == []         # nothing public until approved
    static.approve_all(con, "states"); static.approve_all(con, "banking-terms")
    build_site(con, tmp_path / "s1")
    data = json.loads((tmp_path / "s1" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
    assert {c["slug"] for c in data["static"]} == {"states", "banking-terms"}
    capitals = {i["data"]["name"]: i["data"]["capital"] for c in data["static"] if c["slug"] == "states" for i in c["items"]}
    qs = [q for q in data["static_questions"] if q["cat"] == "States & UTs"]
    assert qs and all(len(set(q["options"])) == 4 for q in qs)
    for q in qs:
        state = q["q"].replace("What is the capital of ", "").rstrip("?")
        assert q["options"][q["answer"]] == capitals[state]                # the keyed answer is the real row value
    assert any(q["cat"] == "Banking Terms" for q in data["static_questions"])

def test_fast_changing_entries_are_not_used_in_quizzes(tmp_path):
    con = fresh(tmp_path)
    for m in ("A", "B", "C", "D", "E"):
        static.add_item(con, "union-cabinet", dict(ministry="M" + m, minister="P" + m, since="2024"), status="approved")
    for k in ("A", "B", "C", "D", "E"):
        static.add_item(con, "awards", dict(award="Aw" + k, field="f", given_by="G" + k), status="approved")
    build_site(con, tmp_path / "s")
    data = json.loads((tmp_path / "s" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
    assert {q["cat"] for q in data["static_questions"]} == {"Awards & Honours"}   # cabinet (volatile) never becomes a quiz

def test_custom_category_and_generic_quiz(tmp_path):
    con = fresh(tmp_path)
    slug = static.add_category(con, "Rivers", ["River", "Origin", "Length km"])
    assert slug == "rivers"
    for r, o in (("Ganga", "Gangotri"), ("Yamuna", "Yamunotri"), ("Godavari", "Nashik"), ("Krishna", "Mahabaleshwar"), ("Narmada", "Amarkantak")):
        static.add_item(con, "rivers", {"river": r, "origin": o}, status="approved")
    for bad in (("A", ["only"]), ("Rivers", ["a", "b"])):
        try: static.add_category(con, bad[0], bad[1]); assert False
        except ValueError: pass
    build_site(con, tmp_path / "s")
    data = json.loads((tmp_path / "s" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
    q = [x for x in data["static_questions"] if x["cat"] == "Rivers"]
    assert len(q) == 5 and all("Origin of" in x["q"] for x in q)

def test_static_admin_api(tmp_path):
    con = fresh(tmp_path)
    srv = make_server(0, db_path=str(tmp_path / "t.db"), site_dir=tmp_path / "site"); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _call(port, "/api/static/items?cat=nope")[0] == 400
        assert _call(port, "/api/static/seed", {})[1]["added"] > 60
        assert _call(port, "/api/static/approve-all", {"cat": "states"})[1]["approved"] == 36
        data = json.loads((tmp_path / "site" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
        assert [c["slug"] for c in data["static"]] == ["states"]               # site rebuilt automatically after approval
        r = _call(port, "/api/static/item", dict(action="create_approved", cat="countries", data=dict(country="Nepal", capital="Kathmandu", currency="Nepalese Rupee", continent="Asia"), as_on="2026-10-01", source="official"))[1]
        assert "id" in r
        assert _call(port, "/api/static/item", dict(action="create_approved", cat="countries", data=dict(country="", capital="x")))[0] == 400
        i = r["id"]
        assert _call(port, "/api/static/item", dict(action="save", id=i, cat="countries", data=dict(country="Nepal", capital="Kathmandu", currency="Nepali Rupee", continent="Asia")))[1]["ok"]
        h = _call(port, f"/api/static/history?id={i}")[1]
        assert len(h) == 2 and h[0]["new"]["data"]["currency"] == "Nepali Rupee"
        imp = _call(port, "/api/static/import", dict(cat="books-authors", csv="book,author,genre\nGitanjali,Rabindranath Tagore,Poetry", approve=True))[1]
        assert imp["added"] == 1
        assert _call(port, "/api/static/cat", dict(name="Dams", kind="table", columns=["dam", "river"]))[1]["slug"] == "dams"
        assert any(c["slug"] == "dams" for c in _call(port, "/api/static/cats")[1])
    finally:
        srv.shutdown()

# ---------- policy-rate watcher + AI drafting for Static GK ----------
from gk import rates, static_ai, fetch as _fetch
from gk.schemas import RateStatement, RateItem, DraftRows

REPO_Q = "Accordingly, the MPC decided to reduce the policy repo rate under the liquidity adjustment facility (LAF) by 25 basis points from 5.50 per cent to 5.25 per cent with immediate effect."
CORR_Q = "Consequently, the standing deposit facility (SDF) rate stands adjusted to 5.00 per cent and the marginal standing facility (MSF) rate and the Bank Rate to 5.50 per cent."
STATEMENT = "Statement by the Governor. " + REPO_Q + " " + CORR_Q + " The MPC also decided to remain accommodative."

def _stmt(repo=5.25, sdf=5.00, msf=5.50, bank=5.50, announced="2026-10-08", repo_quote=REPO_Q, sdf_quote=CORR_Q):
    return RateStatement(is_rate_announcement=True, announced_on=announced, rates={
        "repo_rate": RateItem(value=repo, quote=repo_quote), "sdf_rate": RateItem(value=sdf, quote=sdf_quote),
        "msf_rate": RateItem(value=msf, quote=CORR_Q), "bank_rate": RateItem(value=bank, quote=CORR_Q)})

class RateLLM(MockLLM):
    def __init__(self, stmt=None): super().__init__(); self.stmt = stmt or _stmt()
    def structured(self, prompt, schema, tier="strong", system=""):
        return self.stmt if schema is RateStatement else super().structured(prompt, schema, tier, system)

def _terms(tmp_path):
    con = fresh(tmp_path); static.seed_starter(con); static.approve_all(con, "banking-terms")
    return con

def _cv(con, prefix):
    return next(i for i in static.list_items(con, "banking-terms") if i["data"]["term"].lower().startswith(prefix))

def test_verified_rate_announcement_updates_banking_terms_automatically(tmp_path):
    con = _terms(tmp_path)
    res = rates.process_text(con, RateLLM(), STATEMENT, "https://rbi.org.in/pr/1", "2026-10-09")
    assert res["status"] == "applied" and len(res["applied"]) == 4 and not res["problems"]
    repo = _cv(con, "repo rate")
    assert repo["data"]["current_value"] == "5.25%" and repo["as_on"] == "2026-10-08" and repo["source"] == "https://rbi.org.in/pr/1"
    assert [_cv(con, p)["data"]["current_value"] for p in ("sdf", "msf", "bank rate")] == ["5.00%", "5.50%", "5.50%"]
    assert _cv(con, "reverse repo")["data"]["current_value"] == ""              # not stated in the statement -> untouched
    n_hist = len(static.history(con, repo["id"]))
    assert "rate update" in static.history(con, repo["id"])[0]["note"]
    again = rates.process_text(con, RateLLM(), STATEMENT, "https://rbi.org.in/pr/1", "2026-10-09")
    assert again["status"] == "unchanged" and len(static.history(con, repo["id"])) == n_hist   # same announcement twice: no new history

def test_doubtful_rates_wait_for_approval(tmp_path):
    con = _terms(tmp_path)
    bad_corridor = rates.process_text(con, RateLLM(_stmt(sdf=5.10, sdf_quote=CORR_Q.replace("5.00", "5.10"))), STATEMENT.replace("5.00 per cent", "5.10 per cent"), "u", "2026-10-09")
    assert bad_corridor["status"] == "pending" and any("SDF" in p for p in bad_corridor["problems"])
    assert _cv(con, "repo rate")["data"]["current_value"] == ""                    # nothing applied while rates disagree
    fake_quote = rates.process_text(con, RateLLM(_stmt(repo_quote="The MPC cut the repo rate to 5.25 per cent.")), STATEMENT, "u", "2026-10-09")
    assert any("not in the official text" in p for p in fake_quote["problems"]) and _cv(con, "repo rate")["data"]["current_value"] == ""
    out_of_range = rates.process_text(con, RateLLM(_stmt(repo=52.5, repo_quote=REPO_Q.replace("5.25", "52.5"))), STATEMENT.replace("5.25", "52.5"), "u", "2026-10-09")
    assert any("outside the usual range" in p for p in out_of_range["problems"])
    wrong_number = rates.process_text(con, RateLLM(_stmt(repo=5.75)), STATEMENT, "u", "2026-10-09")
    assert any("does not contain 5.75" in p for p in wrong_number["problems"])
    pend = rates.list_updates(con, "pending")
    assert pend and all(p["status"] == "pending" for p in pend)
    rates.process_text(con, RateLLM(), STATEMENT, "https://rbi.org.in/pr/2", "2026-10-09", auto=False)   # auto-apply switched off
    pend = rates.list_updates(con, "pending"); assert {p["rate_key"] for p in pend} == {"repo_rate", "sdf_rate", "msf_rate", "bank_rate"}
    assert _cv(con, "repo rate")["data"]["current_value"] == ""
    repo_id = next(p["id"] for p in pend if p["rate_key"] == "repo_rate")
    rates.review(con, repo_id, "approve")
    assert _cv(con, "repo rate")["data"]["current_value"] == "5.25%"
    rates.review(con, next(p["id"] for p in pend if p["rate_key"] == "sdf_rate"), "reject")
    assert _cv(con, "sdf")["data"]["current_value"] == ""
    try: rates.review(con, repo_id, "approve"); assert False                         # already decided: cannot be applied twice
    except ValueError: pass

def test_watcher_reads_each_new_rbi_release_once_and_ignores_others(tmp_path):
    con = _terms(tmp_path)
    def art(url, title, source, tier, day="2026-10-08"):
        con.execute("INSERT INTO articles(url,source,tier,title,text,fetched_on) VALUES(?,?,?,?,?,?)", (url, source, tier, title, "short summary", day))
    art("https://rbi.org.in/pr/mpc", "Monetary Policy Statement, 2026-27: Resolution of the Monetary Policy Committee", "rbi_press", 1)
    art("https://rbi.org.in/pr/vrrr", "RBI to conduct 14-day VRRR auction", "rbi_press", 1)
    art("https://news/x", "RBI may cut the repo rate", "the_hindu_national", 2)
    art("https://rbi.org.in/pr/old", "Monetary Policy Statement (old)", "rbi_press", 1, day="2026-09-01"); con.commit()
    seen = []
    def fetcher(url): seen.append(url); return STATEMENT
    out = rates.watch(con, RateLLM(), "2026-10-09", fetcher=fetcher)
    assert [o["url"] for o in out] == ["https://rbi.org.in/pr/mpc"] and seen == ["https://rbi.org.in/pr/mpc"]   # only the matching tier-1 RBI release
    assert _cv(con, "repo rate")["data"]["current_value"] == "5.25%"
    assert rates.watch(con, RateLLM(), "2026-10-09", fetcher=fetcher) == []                              # processed once
    class NotRates(MockLLM): pass
    art("https://rbi.org.in/pr/crr-notice", "Notice about CRR maintenance procedure", "rbi_press", 1); con.commit()
    out = rates.watch(con, NotRates(), "2026-10-09", fetcher=lambda u: "Procedural notice.")
    assert out[0]["status"] == "ignored"

def test_daily_run_updates_rates_when_the_feed_has_an_rbi_statement(tmp_path, monkeypatch):
    con = _terms(tmp_path)
    (tmp_path / "rbi.xml").write_text('''<?xml version="1.0"?><rss version="2.0"><channel><title>RBI</title>
<item><title>Monetary Policy Statement, 2026-27: Resolution of the Monetary Policy Committee</title><link>https://rbi.org.in/pr/9</link>
<pubDate>Mon, 05 Oct 2026 06:00:00 GMT</pubDate><description>The MPC met.</description></item></channel></rss>''')
    (tmp_path / "src.yaml").write_text(f"sources:\n  - {{id: rbi_press, name: RBI, tier: 1, rss: '{tmp_path / 'rbi.xml'}', enabled: true}}\navoid_keywords: []\nlenses: [UPSC_PSC]\n")
    monkeypatch.setattr(_fetch, "fulltext", lambda url: STATEMENT)
    s = pipeline.run_daily("2026-10-05", sources_path=str(tmp_path / "src.yaml"), con=con, llm=RateLLM())
    assert s["rate_updates"] == 4 and _cv(con, "repo rate")["data"]["current_value"] == "5.25%"

def test_ai_drafts_stable_facts_as_pending_and_refuses_fast_changing_ones(tmp_path):
    con = fresh(tmp_path)
    class Rows(MockLLM):
        def __init__(self, rows): super().__init__(); self.rows = rows
        def structured(self, prompt, schema, tier="strong", system=""):
            return DraftRows(rows=self.rows) if schema is DraftRows else super().structured(prompt, schema, tier, system)
    paris = {"Country": "France", "capital": "Paris", "currency": "Euro", "continent": "Europe"}
    r = static_ai.draft_from_knowledge(con, Rows([paris, {"country": "", "capital": "x"}, dict(paris)]), "countries", "Europe")
    assert r == {"added": 1, "skipped": 1, "dropped": 1}
    it = static.list_items(con, "countries")[0]
    assert it["status"] == "pending" and it["source"].startswith("AI draft") and it["data"]["capital"] == "Paris"
    for slug in ("union-cabinet", "india-indices"):
        try: static_ai.draft_from_knowledge(con, Rows([]), slug); assert False
        except ValueError: pass
    static_ai.draft_from_knowledge(con, Rows([{"term": "Call Money", "definition": "Overnight lending between banks", "current_value": "9.99%"}]), "banking-terms")
    assert next(i for i in static.list_items(con, "banking-terms") if i["data"]["term"] == "Call Money")["data"]["current_value"] == ""   # AI never fills rates

def test_extraction_keeps_only_rows_backed_by_the_pasted_text(tmp_path):
    con = fresh(tmp_path)
    text = "Official release: India ranked 105 out of 127 countries in the Global Hunger Index 2024, published by Welthungerhilfe and Concern Worldwide."
    class Ext(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is DraftRows:
                return DraftRows(rows=[dict(index="Global Hunger Index", publisher="Welthungerhilfe and Concern Worldwide", year="2024", india_rank="105", total_countries="127"),
                                       dict(index="Press Freedom Index", india_rank="151"),                      # not in the text
                                       dict(index="Global Hunger Index", year="2024", india_rank="999")])        # number not in the text
            return super().structured(prompt, schema, tier, system)
    r = static_ai.extract_from_text(con, Ext(), "india-indices", text, "https://example.org/ghi")
    assert r == {"added": 1, "skipped": 0, "dropped": 2}
    it = static.list_items(con, "india-indices")[0]
    assert it["status"] == "pending" and it["data"]["india_rank"] == "105" and it["source"] == "https://example.org/ghi"
    try: static_ai.extract_from_text(con, Ext(), "india-indices", "too short"); assert False
    except ValueError: pass

def test_admin_api_for_rates_and_ai_drafts(tmp_path):
    con = _terms(tmp_path)
    srv = make_server(0, db_path=str(tmp_path / "t.db"), site_dir=tmp_path / "site", llm_factory=lambda: RateLLM()); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cats = {c["slug"]: c for c in _call(port, "/api/static/cats")[1]}
        assert cats["union-cabinet"]["knowledge_ok"] is False and cats["countries"]["knowledge_ok"] and "Asia" in cats["countries"]["hints"]
        assert _call(port, "/api/static/rates-process", {"text": "short"})[0] == 400
        r = _call(port, "/api/static/rates-process", {"text": STATEMENT, "url": "https://rbi.org.in/pr/77"})[1]
        assert r["status"] == "applied" and len(r["applied"]) == 4
        assert {u["status"] for u in _call(port, "/api/static/rates")[1]} == {"applied"}
        data = json.loads((tmp_path / "site" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
        repo = next(i for c in data["static"] if c["slug"] == "banking-terms" for i in c["items"] if i["data"]["term"] == "Repo Rate")
        assert repo["data"]["current_value"] == "5.25%" and repo["source"] == "https://rbi.org.in/pr/77"      # live on the site, with a source link
        assert _call(port, "/api/static/rates-review", {"id": 1, "action": "approve"})[0] == 400                # nothing pending
        assert _call(port, "/api/static/draft", {"cat": "union-cabinet", "mode": "knowledge"})[0] == 400
        assert _call(port, "/api/static/draft", {"cat": "countries", "mode": "knowledge", "hint": "Asia", "n": 3})[1]["added"] == 3
    finally:
        srv.shutdown()

# ---------- automatic filling of Static GK ----------
from gk.llm import QuotaExhausted as _QE

def test_fill_next_drafts_batches_in_order_marks_them_done_and_respects_limits(tmp_path):
    con = fresh(tmp_path); prompts_seen = []
    class Spy(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is DraftRows: prompts_seen.append(prompt)
            return super().structured(prompt, schema, tier, system)
    r = static_ai.fill_next(con, Spy(), max_calls=3, n=30, day="2026-10-17")
    assert r["calls"] == 3 and r["added"] == 9 and [x.split(":")[1].split(" (")[0].strip() for x in r["drafted"]] == ["Asia", "Europe", "Africa"]
    assert all(i["status"] == "pending" and i["source"].startswith("AI draft") for i in static.list_items(con, "countries"))
    r2 = static_ai.fill_next(con, Spy(), max_calls=2, n=30, day="2026-10-18")
    assert [x.split(":")[1].split(" (")[0].strip() for x in r2["drafted"]] == ["North America", "South America"]     # continues, never repeats
    for _ in range(30): static_ai.fill_next(con, Spy(), max_calls=5)
    assert static_ai.fill_next(con, Spy(), max_calls=5)["calls"] == 0                                        # everything drafted once
    assert not any("union-cabinet" in d or "india-indices" in d for d in [r["detail"] for r in con.execute("SELECT detail FROM runs WHERE stage='static_fill'")])
    assert any("Draft 12 NEW" in p for p in prompts_seen) and any("Draft 30 NEW" in p for p in prompts_seen)   # glossary batches are shorter

def test_fill_stops_cleanly_when_the_ai_quota_runs_out(tmp_path):
    con = fresh(tmp_path)
    class Quota(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is DraftRows and self.calls >= 1: raise _QE("daily quota used up")
            self.calls += 1
            return super().structured(prompt, schema, tier, system)
    r = static_ai.fill_next(con, Quota(), max_calls=5, n=10)
    assert r["calls"] == 1 and r["stopped"] == "daily quota used up"
    assert con.execute("SELECT COUNT(*) FROM runs WHERE stage='static_fill'").fetchone()[0] == 1               # only the finished batch is marked done

def test_daily_run_quietly_adds_static_drafts_for_real_providers_only(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "STATIC_AUTOFILL", 2)
    con = fresh(tmp_path)
    s = pipeline.run_daily("2026-10-05", mock=True, sources_path=DEMO, con=con)
    assert "static_drafted" not in s and static.list_items(con, "countries") == []                            # demo/mock mode never writes drafts
    class Real(MockLLM): provider = "gemini"
    s = pipeline.run_daily("2026-10-06", sources_path=DEMO, con=fresh(tmp_path), llm=Real())
    assert s["static_drafted"] == 6
    monkeypatch.setattr(_cfg, "STATIC_AUTOFILL", 0)
    con2 = _db.connect(str(tmp_path / "t.db"))
    s = pipeline.run_daily("2026-10-07", sources_path=DEMO, con=con2, llm=Real())
    assert "static_drafted" not in s                                                                         # switched off

def test_fill_button_api(tmp_path):
    fresh(tmp_path)
    n = [0]
    def factory():                                   # each request gets a fresh model object; offset its counter so mock rows stay unique
        n[0] += 100; m = MockLLM(); m.calls = n[0]; return m
    srv = make_server(0, db_path=str(tmp_path / "t.db"), site_dir=tmp_path / "site", llm_factory=factory); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = _call(port, "/api/static/fill", {"calls": 2})[1]
        assert r["calls"] == 2 and r["added"] == 6 and len(r["drafted"]) == 2
        r = _call(port, "/api/static/fill", {"calls": 2})[1]
        assert r["drafted"][0].startswith("Countries: Africa")                                               # next batch, not a repeat
        assert len(_call(port, "/api/static/items?cat=countries&status=pending")[1]) == 12
    finally:
        srv.shutdown()

# ---------- committees section ----------
def test_committees_category_has_the_requested_columns_and_newest_first_default(tmp_path):
    con = fresh(tmp_path)
    c = next(x for x in static.overview(con) if x["slug"] == "committees")
    assert c["columns"] == ["committee", "established", "functions", "current_head"] and c["name"] == "Committees & Commissions"
    assert static.DEFAULT_SORT["committees"] == {"col": "established", "dir": "desc"}
    static.seed_starter(con); static.approve_all(con, "committees"); static.approve_all(con, "states")
    build_site(con, tmp_path / "s")
    data = json.loads((tmp_path / "s" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
    cat = next(x for x in data["static"] if x["slug"] == "committees")
    assert cat["sort"] == {"col": "established", "dir": "desc"} and len(cat["items"]) >= 15
    fc = next(i for i in cat["items"] if i["data"]["committee"] == "Finance Commission")
    assert fc["data"]["current_head"] == "" and fc["data"]["established"] == "1951"        # live bodies: head left for the admin
    assert next(x for x in data["static"] if x["slug"] == "states")["sort"] is None          # other tables keep their own order

def test_new_columns_are_added_to_existing_categories_without_losing_data(tmp_path):
    p = str(tmp_path / "old.db"); con = _db.connect(p)
    con.execute("UPDATE static_cats SET columns_json=? WHERE slug='committees'", (json.dumps(["committee", "chairperson"]),)); con.commit()
    i = static.add_item(con, "committees", {"committee": "Old Panel", "chairperson": "X"}, status="approved")
    con2 = _db.connect(p)                                                                      # next start-up after the update
    cols = json.loads(con2.execute("SELECT columns_json FROM static_cats WHERE slug='committees'").fetchone()[0])
    assert cols == ["committee", "chairperson", "established", "functions", "current_head"]
    static.update_item(con2, i, data={"committee": "Old Panel", "established": "1999"})
    assert static.list_items(con2, "committees")[0]["data"]["established"] == "1999"

def test_committee_heads_are_never_drafted_from_memory_and_quiz_asks_for_the_year(tmp_path):
    con = fresh(tmp_path)
    class Rows(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is DraftRows:
                return DraftRows(rows=[dict(committee=f"Panel {i}", established=str(1950 + i), functions="f", current_head="Dr Made Up") for i in range(1, 6)])
            return super().structured(prompt, schema, tier, system)
    assert static_ai.draft_from_knowledge(con, Rows(), "committees", "Parliamentary committees")["added"] == 5
    assert all(i["data"]["current_head"] == "" for i in static.list_items(con, "committees"))
    static.approve_all(con, "committees"); build_site(con, tmp_path / "s")
    data = json.loads((tmp_path / "s" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
    q = [x for x in data["static_questions"] if x["cat"] == "Committees & Commissions"]
    assert q and all(x["q"].startswith("In which year was the") for x in q)                    # asks the stable fact, not the changing head
    assert "committees" in static_ai.FILL_ORDER and static_ai.HINTS["committees"]

# ---------- stale / republished news ----------
import re
from gk.schemas import RelevanceBatch, BatchItem

def _feed(tmp_path, items):
    body = "".join(f"<item><title>{t}</title><link>https://x/{i}</link><pubDate>{d}</pubDate><description>Text {i}.</description></item>" for i, (t, d) in enumerate(items))
    (tmp_path / "f.xml").write_text(f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>')
    return {"sources": [{"id": "s", "tier": 2, "rss": str(tmp_path / "f.xml"), "enabled": True}], "avoid_keywords": []}

def test_default_window_is_today_and_yesterday_and_the_real_publication_day_is_stored(tmp_path):
    assert _cfg.MAX_AGE_DAYS == 2
    con = fresh(tmp_path)
    cfg = _feed(tmp_path, [("Reported today", "Mon, 05 Oct 2026 06:00:00 GMT"), ("Reported yesterday", "Sun, 04 Oct 2026 20:00:00 GMT"),
                           ("Reported three days ago", "Fri, 02 Oct 2026 08:00:00 GMT")])
    assert fetch.fetch_all(con, "2026-10-05", cfg) == 2
    assert {r["title"]: r["pub_day"] for r in con.execute("SELECT title, pub_day FROM articles")} == {"Reported today": "2026-10-05", "Reported yesterday": "2026-10-04"}

def test_events_older_than_the_limit_are_dropped_even_if_republished_today(tmp_path):
    con = fresh(tmp_path); seen = {}
    for t, d in (("Fresh story", "2026-10-05"), ("Old RBI decision explained", "2026-10-05"), ("Story with unknown age", "2026-10-05")):
        _art(con, t, day="2026-10-05")
    con.execute("UPDATE articles SET pub_day='2026-10-04' WHERE title='Fresh story'"); con.commit()
    class Ages(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is not RelevanceBatch: return super().structured(prompt, schema, tier, system)
            seen["prompt"] = prompt
            age = {"Fresh story": 0, "Old RBI decision explained": 12, "Story with unknown age": None}
            ids = re.findall(r"^\[(\d+)\] .*?\) (.*?) ::", prompt, re.M)
            return RelevanceBatch(items=[BatchItem(id=int(i), keep=True, score=9, sector="Economy", lenses=[], event_age_days=age[t]) for i, t in ids])
    analyze.score_articles(con, Ages(), "2026-10-05", {"lenses": []})
    st = {r["title"]: (r["status"], r["event_age"]) for r in con.execute("SELECT title, status, event_age FROM articles")}
    assert st["Fresh story"][0] == "kept" and st["Story with unknown age"][0] == "kept"
    assert st["Old RBI decision explained"] == ("dropped", 12)                         # old event never reaches the notes
    assert "Today is 2026-10-05" in seen["prompt"] and "published 2026-10-04" in seen["prompt"]   # the AI is told the dates

def test_reported_date_reaches_the_site_only_when_it_differs_from_the_digest_day(tmp_path):
    con = fresh(tmp_path)
    a = _art(con, "Reported earlier story headline words here", day="2026-10-19", cid=1, status="kept"); b = _art(con, "Same day story headline words there", day="2026-10-19", cid=2, status="kept")
    con.execute("UPDATE articles SET pub_day='2026-10-17' WHERE id=?", (a,)); con.execute("UPDATE articles SET pub_day='2026-10-19' WHERE id=?", (b,)); con.commit()
    for cid, h in ((1, "Earlier"), (2, "Same")):
        con.execute("INSERT INTO notes(cluster_id,day,sector,headline,body_json,importance,status,source_urls) VALUES(?,?,?,?,?,?,?,?)",
                    (cid, "2026-10-19", "Economy", h, json.dumps(dict(headline=h, sector="Economy", bullets=["b"], exam_angle="", importance="High", claims=[])), "High", "approved", "[]"))
    con.commit(); build_site(con, tmp_path / "s")
    data = json.loads((tmp_path / "s" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
    assert {n["headline"]: n["reported"] for n in data["notes"]} == {"Earlier": "2026-10-17", "Same": None}

def test_duplicate_check_remembers_notes_from_the_last_ten_days(tmp_path):
    assert _cfg.EVENT_MERGE_DAYS == 10
    con = fresh(tmp_path); seen = {}
    con.execute("INSERT INTO notes(cluster_id,day,sector,headline,body_json,importance) VALUES(1,'2026-10-11','Economy','RBI hikes repo rate to 5.50%','{}','High')")
    _art(con, "RBI Increases Repo Rate to 5.50%", day="2026-10-19", cid=2, status="kept")
    class Spy(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is EventGroups: seen["p"] = prompt; return EventGroups(groups=[[1, 2]])
            return super().structured(prompt, schema, tier, system)
    assert analyze.merge_events(con, Spy(), "2026-10-19") == 1                          # the 8-day-old coverage is found and the repeat is folded into it
    assert "(already covered) RBI hikes repo rate to 5.50%" in seen["p"]
    assert analyze.waiting_clusters(con) == []                                           # no second note for the same event

# ---------- Important Bills ----------
def test_important_bills_category_columns_sort_and_starter_set(tmp_path):
    con = fresh(tmp_path)
    c = next(x for x in static.overview(con) if x["slug"] == "important-bills")
    assert c["columns"] == ["bill", "passed_year", "passed_by", "importance", "background"] and c["name"] == "Important Bills"
    assert static.DEFAULT_SORT["important-bills"] == {"col": "passed_year", "dir": "desc"}            # current to past
    static.seed_starter(con)
    rows = static.list_items(con, "important-bills")
    assert len(rows) >= 20 and all(r["status"] == "pending" and re.fullmatch(r"\d{4}", r["data"]["passed_year"]) for r in rows)
    assert all(1950 <= int(r["data"]["passed_year"]) <= 2025 and r["data"]["passed_by"].startswith("Parliament of India") for r in rows)
    caa = next(r for r in rows if "Citizenship (Amendment) Act, 2019" in r["data"]["bill"])
    assert caa["data"]["passed_year"] == "2019" and caa["data"]["importance"] and caa["data"]["background"]
    static.approve_all(con, "important-bills"); build_site(con, tmp_path / "s")
    data = json.loads((tmp_path / "s" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
    cat = next(x for x in data["static"] if x["slug"] == "important-bills")
    assert cat["sort"] == {"col": "passed_year", "dir": "desc"}
    assert any(q["q"].startswith("In which year was the") and q["cat"] == "Important Bills" for q in data["static_questions"])

def test_bill_drafts_stay_inside_the_requested_years_and_run_from_current_to_past(tmp_path):
    con = fresh(tmp_path); seen = []
    class Bills(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is DraftRows:
                seen.append(prompt)
                bill = lambda n, y: dict(bill=n, passed_year=y, passed_by="Parliament of India (X government)", importance="i", background="b")
                return DraftRows(rows=[bill("In range", "2021"), bill("Too old", "2012"), bill("Too new", "2025"), bill("Not a year", "recently"), bill("No year", "")])
            return super().structured(prompt, schema, tier, system)
    r = static_ai.draft_from_knowledge(con, Bills(), "important-bills", "2019-2023")
    assert r["added"] == 1 and r["dropped"] == 4
    assert [i["data"]["bill"] for i in static.list_items(con, "important-bills")] == ["In range"]
    assert "Guidance for this category:" in seen[0] and "ONE short sentence" in seen[0]
    # automatic filling goes newest period first, and never auto-drafts the 'AI may be out of date' recent period
    con2 = fresh(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    out = static_ai.fill_next(con2, MockLLM(), max_calls=200)
    order = [x.split(": ")[1].split(" (+")[0] for x in out["drafted"] if x.startswith("Important Bills")]
    assert order == ["2019-2023", "2014-2018", "2004-2013", "1991-2003", "1950-1990"]
    assert static_ai.RECENT_BILLS in static_ai.HINTS["important-bills"] and ("important-bills", static_ai.RECENT_BILLS) in static_ai.AUTO_SKIP

def test_recent_bills_can_be_extracted_from_an_official_list(tmp_path):
    con = fresh(tmp_path)
    text = "Parliament passed The Example Bill, 2026 in March 2026. It creates a new authority for example purposes under the Modi government."
    class Ext(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is DraftRows:
                return DraftRows(rows=[dict(bill="The Example Bill, 2026", passed_year="2026", passed_by="Parliament of India", importance="creates an authority", background="x"),
                                       dict(bill="The Invented Bill, 2025", passed_year="2025")])
            return super().structured(prompt, schema, tier, system)
    r = static_ai.extract_from_text(con, Ext(), "important-bills", text, "https://prsindia.org/example")
    assert r["added"] == 1 and r["dropped"] == 1                                   # the invented bill is not in the pasted text

# ---------- Sobriquets ----------
def test_sobriquets_category_starter_set_covers_all_five_types(tmp_path):
    con = fresh(tmp_path)
    c = next(x for x in static.overview(con) if x["slug"] == "sobriquets")
    assert c["columns"] == ["sobriquet", "given_to", "type", "field", "why_known"] and static.DEFAULT_FILTER["sobriquets"] == "type"
    static.seed_starter(con)
    rows = static.list_items(con, "sobriquets")
    assert len(rows) >= 90 and all(r["status"] == "pending" for r in rows)
    assert {r["data"]["type"] for r in rows} == {"Person", "Place", "Event", "Plant", "Animal"}
    assert all(r["data"]["sobriquet"] and r["data"]["given_to"] for r in rows)
    patel = next(r for r in rows if r["data"]["sobriquet"] == "Iron Man of India")
    assert (patel["data"]["given_to"], patel["data"]["type"]) == ("Sardar Vallabhbhai Patel", "Person")
    assert len({(r["data"]["sobriquet"], r["data"]["given_to"]) for r in rows}) == len(rows)          # no duplicates

def test_sobriquet_quiz_options_come_from_the_same_type_and_never_include_another_right_answer(tmp_path):
    con = fresh(tmp_path); static.seed_starter(con)
    static.add_item(con, "sobriquets", dict(sobriquet="Nightingale of India", given_to="Lata Mangeshkar", type="Person", field="Music"))   # a second right answer
    static.approve_all(con, "sobriquets"); build_site(con, tmp_path / "s")
    data = json.loads((tmp_path / "s" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])
    by_type = {i["data"]["given_to"]: i["data"]["type"] for c in data["static"] if c["slug"] == "sobriquets" for i in c["items"]}
    qs = [q for q in data["static_questions"] if q["cat"] == "Sobriquets" and q["q"].startswith("Who or what is known as")]
    assert qs
    for q in qs:
        answer = q["options"][q["answer"]]
        assert {by_type[o] for o in q["options"]} == {by_type[answer]}                       # a person question never offers a city as an option
        if "Nightingale of India" in q["q"]:
            assert not ({"Sarojini Naidu", "Lata Mangeshkar"} & set(q["options"])) - {answer}  # the other right answer is not a wrong option
    assert next(c for c in data["static"] if c["slug"] == "sobriquets")["filter"] == "type"

def test_sobriquet_drafts_need_one_of_the_five_types(tmp_path):
    con = fresh(tmp_path)
    class Rows(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is DraftRows:
                return DraftRows(rows=[dict(sobriquet="Pink City", given_to="Jaipur", type="place", field="Rajasthan"),
                                       dict(sobriquet="Mystery", given_to="Someone", type="Robot"), dict(sobriquet="No target", given_to="", type="Person")])
            return super().structured(prompt, schema, tier, system)
    r = static_ai.draft_from_knowledge(con, Rows(), "sobriquets", "Cities and regions of India")
    assert r == {"added": 1, "skipped": 0, "dropped": 2}
    assert static.list_items(con, "sobriquets")[0]["data"]["type"] == "Place"                  # capitalisation normalised
    assert "sobriquets" in static_ai.FILL_ORDER and len(static_ai.HINTS["sobriquets"]) == 8 and "EXACTLY one of Person, Place, Event, Plant, Animal" in static_ai.GUIDE["sobriquets"]

# ---------- schemes, Chief Justices, Presidents ----------
def _site_data(tmp_path, con, slugs):
    for s in slugs: static.approve_all(con, s)
    build_site(con, tmp_path / "site")
    return json.loads((tmp_path / "site" / "data.js").read_text(encoding="utf-8")[len("window.GK_DATA="):-1])

def test_new_categories_have_the_requested_columns_sort_and_filter(tmp_path):
    con = fresh(tmp_path); cats = {c["slug"]: c for c in static.overview(con)}
    assert cats["schemes"]["columns"] == ["scheme", "start_date", "sector", "purpose", "place", "description"] and cats["schemes"]["name"] == "Government Schemes"
    assert cats["chief-justices"]["columns"] == ["chief_justice", "no", "appointed_year", "tenure", "major_decisions"]
    assert cats["presidents"]["columns"] == ["president", "no", "took_office", "tenure", "major_decisions"]
    assert static.DEFAULT_FILTER["schemes"] == "sector"
    assert [static.DEFAULT_SORT[s]["dir"] for s in ("schemes", "chief-justices", "presidents")] == ["desc"] * 3       # latest to past
    assert {"chief-justices", "presidents"} <= static_ai.TEXT_ONLY and "schemes" in static_ai.FILL_ORDER

def test_starter_lists_are_complete_and_internally_consistent(tmp_path):
    con = fresh(tmp_path); static.seed_starter(con)
    cji = [r["data"] for r in static.list_items(con, "chief-justices")]
    assert sorted(int(c["no"]) for c in cji) == list(range(1, 54))                                      # 53 Chief Justices, none missing
    for c in cji:                                                                                       # appointed year matches the start of the tenure
        assert c["tenure"].split(" - ")[0].split()[-1] == c["appointed_year"]
    by_no = {int(c["no"]): c for c in cji}
    assert by_no[1]["chief_justice"] == "H. J. Kania" and by_no[13]["major_decisions"].startswith("Kesavananda Bharati") and by_no[50]["appointed_year"] == "2022"
    years = [int(by_no[n]["appointed_year"]) for n in range(1, 54)]
    assert years == sorted(years)                                                                       # chronological
    pres = {int(p["data"]["no"]): p["data"] for p in static.list_items(con, "presidents")}
    assert sorted(pres) == list(range(1, 16)) and pres[1]["president"] == "Rajendra Prasad" and pres[14]["president"] == "Ram Nath Kovind"
    assert all(p["tenure"].split(" - ")[0].split()[-1] == p["took_office"] for p in pres.values())
    sch = [r["data"] for r in static.list_items(con, "schemes")]
    assert len(sch) >= 50 and len({s["scheme"] for s in sch}) == len(sch)
    assert all(re.search(r"(19|20)\d\d", s["start_date"]) and s["purpose"] and s["description"] and s["sector"] for s in sch)
    assert next(s for s in sch if s["scheme"] == "PM-KISAN")["place"].startswith("Gorakhpur")

def test_quiz_questions_for_the_new_sections_are_keyed_to_the_rows(tmp_path):
    con = fresh(tmp_path); static.seed_starter(con)
    data = _site_data(tmp_path, con, ["schemes", "chief-justices", "presidents"])
    rows = {c["slug"]: c["items"] for c in data["static"]}
    launched = {i["data"]["scheme"]: i["data"]["start_date"] for i in rows["schemes"]}
    dq = [q for q in data["static_questions"] if q["cat"] == "Government Schemes" and q["q"].startswith("When was the")]
    assert dq and all(q["options"][q["answer"]] == launched[q["q"][len("When was the "):-len(" launched?")]] for q in dq)   # every date question is keyed to its row
    qs = [q for q in data["static_questions"] if q["cat"] == "Chief Justices of India"]
    assert qs and all(q["q"].startswith("In which year did") for q in qs)
    year = {i["data"]["chief_justice"]: i["data"]["appointed_year"] for i in rows["chief-justices"]}
    assert all(q["options"][q["answer"]] == year[q["q"].split("did ")[1].split(" become")[0]] for q in qs)
    assert any(q["cat"] == "Presidents of India" for q in data["static_questions"])

def test_ai_cannot_draft_the_justice_and_president_lists_and_scheme_dates_must_have_a_year(tmp_path):
    con = fresh(tmp_path)
    class Rows(MockLLM):
        def structured(self, prompt, schema, tier="strong", system=""):
            if schema is DraftRows:
                s = lambda n, d: dict(scheme=n, start_date=d, sector="Health", purpose="p", place="", description="d")
                return DraftRows(rows=[s("Good Scheme", "5 May 2016"), s("No year scheme", "recently"), s("Month only scheme", "May 2016"), dict(scheme="", start_date="2016")])
            return super().structured(prompt, schema, tier, system)
    for slug in ("chief-justices", "presidents"):
        try: static_ai.draft_from_knowledge(con, Rows(), slug); assert False
        except ValueError: pass
    r = static_ai.draft_from_knowledge(con, Rows(), "schemes", "Health and nutrition")
    assert r == {"added": 2, "skipped": 0, "dropped": 2}
    assert "Only real central or major state schemes" in static_ai.GUIDE["schemes"] and len(static_ai.HINTS["schemes"]) == 12
