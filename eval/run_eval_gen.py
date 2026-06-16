"""
Phase 4.5: Generation-quality eval (faithfulness, context-precision,
answer-relevance) on top of the existing recall@k retrieval eval.

Runs each golden / adversarial question through the full LangGraph and
scores the ANSWER (not just the retrieved chunks):

  faithfulness      — fraction of answer claims supported by retrieved
                      chunks (LLM-as-judge; ≈ RAGAS faithfulness)
  context_precision — fraction of retrieved chunks that are relevant
                      (reuses evaluator.grade_retrieval)
  answer_relevance  — does the answer address the question
                      (LLM-as-judge; ≈ RAGAS answer_relevancy)
  scope_correct     — for adversarial set: cited repos ⊆ expected, no
                      forbidden repos/strings, blocked when expected

Usage:
  python eval/run_eval_gen.py                  # both sets
  python eval/run_eval_gen.py --adversarial    # adversarial only
  python eval/run_eval_gen.py --no-evaluator   # ENABLE_EVALUATOR=false A/B
"""

import argparse
import json
import os
import re
import statistics
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _setup_flags(args):
    if args.no_evaluator:
        os.environ["ENABLE_EVALUATOR"] = "false"
    if args.no_guards:
        os.environ["ENABLE_GUARDS"] = "false"
    os.environ.setdefault("ENABLE_MEMORIES", "false")


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _cited_repos(citations: list[dict]) -> set[str]:
    out: set[str] = set()
    for c in citations:
        f = (c.get("file") or "").strip()
        if "/" in f:
            out.add(f.split("/", 1)[0])
    return out


def _scope_check(item: dict, answer: str, citations: list[dict],
                 guard_flags: dict) -> tuple[bool, str]:
    if item.get("expect_blocked"):
        ok = bool(guard_flags.get("blocked"))
        return ok, "blocked" if ok else "NOT blocked"

    repos = _cited_repos(citations)
    notes: list[str] = []

    exp = set(item.get("expected_repos") or [])
    forbid = set(item.get("forbidden_repos") or [])
    if exp and not exp & repos:
        notes.append(f"missing repos {sorted(exp)}")
    if forbid & repos:
        notes.append(f"FORBIDDEN repo cited: {sorted(forbid & repos)}")

    if (n := item.get("min_distinct_repo_citations")) and len(repos) < n:
        notes.append(f"only {len(repos)}/{n} distinct repos cited")
    if (n := item.get("min_citations")) and len(citations) < n:
        notes.append(f"only {len(citations)}/{n} citations")

    for s in item.get("forbidden_answer") or []:
        if s.lower() in answer.lower():
            notes.append(f"forbidden string in answer: {s!r}")
    if (pat := item.get("forbidden_answer_re")) and re.search(pat, answer):
        notes.append(f"forbidden pattern matched: {pat}")

    return (not notes), "; ".join(notes) or "ok"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--adversarial", action="store_true",
                   help="Run only the adversarial set")
    p.add_argument("--no-evaluator", action="store_true")
    p.add_argument("--no-guards", action="store_true")
    args = p.parse_args()

    _setup_flags(args)

    from rag.agents.graph import build_graph
    from rag.agents.nodes.evaluator import grade_retrieval, grade_answer
    from langchain_core.messages import HumanMessage

    here = Path(__file__).parent
    sets: list[tuple[str, list[dict]]] = []
    if not args.adversarial:
        sets.append(("golden", _load(here / "golden_set.jsonl")))
    sets.append(("adversarial", _load(here / "golden_set_adversarial.jsonl")))

    app = build_graph(use_checkpointer=False)

    print(f"Generation eval — evaluator={'OFF' if args.no_evaluator else 'ON'} "
          f"guards={'OFF' if args.no_guards else 'ON'}\n")

    for name, items in sets:
        if not items:
            continue
        print(f"{'─'*70}\n{name.upper()} SET ({len(items)} questions)\n{'─'*70}")
        print(f"{'#':<3} {'Faith':<7} {'CtxPrec':<8} {'AnsRel':<7} "
              f"{'Scope':<6} {'Iter':<5} Question")

        faith, cprec, arel, scope_ok, iters = [], [], [], [], []
        for i, item in enumerate(items, 1):
            q = item["question"]
            cfg = {"configurable": {"thread_id": f"eval-{uuid.uuid4().hex[:8]}"}}
            try:
                result = app.invoke(
                    {"query": q, "messages": [HumanMessage(content=q)],
                     "verbosity": "normal"},
                    config=cfg,
                )
            except Exception as e:
                print(f"{i:<3} ERROR: {type(e).__name__}: {e}")
                continue

            answer = result.get("answer", "")
            citations = result.get("citations") or []
            guard_flags = result.get("guard_flags") or {}
            it = result.get("eval_iter") or result.get("iteration") or 0

            r_scores, _ = grade_retrieval(result)
            a_scores, _ = grade_answer(result)

            f = a_scores.get("groundedness", 0.0)
            cp = (r_scores["n_relevant"] / r_scores["n_chunks"]
                  if r_scores.get("n_chunks") else 0.0)
            ar = a_scores.get("completeness", 0.0)
            sok, note = _scope_check(item, answer, citations, guard_flags)

            faith.append(f); cprec.append(cp); arel.append(ar)
            scope_ok.append(sok); iters.append(it)

            mark = "✓" if sok else "✗"
            qshort = q[:48] + "…" if len(q) > 50 else q
            print(f"{i:<3} {f:<7.2f} {cp:<8.2f} {ar:<7.2f} "
                  f"{mark:<6} {it:<5} {qshort}")
            if not sok:
                print(f"    └─ {note}")

        if faith:
            print(f"\n  mean: faithfulness={statistics.mean(faith):.3f}  "
                  f"context_precision={statistics.mean(cprec):.3f}  "
                  f"answer_relevance={statistics.mean(arel):.3f}  "
                  f"scope_pass={sum(scope_ok)}/{len(scope_ok)}  "
                  f"avg_iter={statistics.mean(iters):.1f}\n")


if __name__ == "__main__":
    main()
