from . import prompts, config
from .schemas import QuizSet, SolverBatch
from .verify import valid_mcq
from .llm import CallBudgetExceeded

def make_quiz(con, llm, note_id, note, n=None):
    """One call writes the questions, ONE call blind-checks all of them (saves daily quota)."""
    if note.importance == "Low":
        return 0
    qs = llm.structured(prompts.QUIZ_PROMPT.format(n=n or config.QUIZ_PER_NOTE, headline=note.headline,
                        bullets="\n".join("- " + b for b in note.bullets)),
                        QuizSet, tier=config.QUIZ_TIER, system=prompts.QUIZ_SYSTEM)
    valid = [m for m in qs.mcqs if valid_mcq(m)]
    agree = {}
    if valid:
        text = "\n".join(f"[{i}] {m.question}\n" + "\n".join(f"   {j}. {o}" for j, o in enumerate(m.options))
                         for i, m in enumerate(valid))
        try:
            res = llm.structured(prompts.SOLVER_BATCH_PROMPT.format(questions=text), SolverBatch,
                                 tier="cheap", system=prompts.SOLVER_SYSTEM)
            agree = {a.id: a.confident and a.chosen_index == valid[a.id].answer_index
                     for a in res.answers if 0 <= a.id < len(valid)}
        except CallBudgetExceeded:
            raise
        except Exception:
            agree = {}                      # check failed: questions are flagged for a human, not dropped
    for m in qs.mcqs:
        ok = valid_mcq(m)
        solver_ok = int(ok and agree.get(valid.index(m), False))
        con.execute("INSERT INTO questions(note_id,body_json,valid,solver_ok,status) VALUES(?,?,?,?,?)",
                    (note_id, m.model_dump_json(), int(ok), solver_ok, "pending"))
    con.commit()
    return len(qs.mcqs)
