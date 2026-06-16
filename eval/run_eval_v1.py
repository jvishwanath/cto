"""
Phase 1/3.5: Eval harness — measures hybrid (dense + native BM25 + rerank)
retrieval quality against the golden set.
Usage: python eval/run_eval_v1.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from rag.retrieval import hybrid_search, rerank

GOLDEN_SET = Path(__file__).parent / "golden_set.jsonl"
K_VALUES = [1, 3, 5, 10]


def load_golden_set() -> list[dict]:
    items = []
    with open(GOLDEN_SET) as f:
        for line in f:
            if line.strip():
                items.append(json.loads(line))
    return items


def evaluate_retrieval(item: dict, k: int) -> dict:
    candidates = hybrid_search(item["question"], top_k=max(k * 3, 30))
    results = rerank(item["question"], candidates, top_k=k)

    retrieved_texts = [r["text"].lower() for r in results]
    retrieved_repos = [r["repo"] for r in results]
    chunk_types = [r["chunk_type"] for r in results]

    repo_hit = any(item["expected_file"] in repo for repo in retrieved_repos)

    keywords = item.get("expected_keywords", [])
    keyword_hits = sum(
        1 for kw in keywords if any(kw.lower() in text for text in retrieved_texts)
    )
    keyword_recall = keyword_hits / len(keywords) if keywords else 0

    return {
        "repo_hit": repo_hit,
        "keyword_recall": keyword_recall,
        "hit": repo_hit and keyword_recall >= 0.5,
        "chunk_types": chunk_types,
    }


def main():
    golden = load_golden_set()
    if not golden:
        print("No eval questions found in golden_set.jsonl")
        sys.exit(1)

    print(f"Running eval on {len(golden)} questions (hybrid: dense + native BM25 + rerank)\n")
    print(f"{'K':<5} {'Recall@K':<12} {'Repo Hit Rate':<16} {'Keyword Recall'}")
    print("-" * 50)

    for k in K_VALUES:
        hits = repo_hits = total_kw = 0
        for item in golden:
            r = evaluate_retrieval(item, k)
            hits += r["hit"]
            repo_hits += r["repo_hit"]
            total_kw += r["keyword_recall"]
        n = len(golden)
        print(f"{k:<5} {hits/n:<12.3f} {repo_hits/n:<16.3f} {total_kw/n:.3f}")

    print(f"\n\nDetail (k=5):")
    print(f"{'Question':<55} {'Hit':<6} {'KW':<6} {'Types'}")
    print("-" * 90)
    for item in golden:
        r = evaluate_retrieval(item, 5)
        q = item["question"][:52] + "..." if len(item["question"]) > 55 else item["question"]
        types = set(r["chunk_types"][:3])
        print(f"{q:<55} {'✓' if r['hit'] else '✗':<6} {r['keyword_recall']:<6.2f} {types}")

    print("\n\n--- Phase 0 baseline for comparison ---")
    print("K     Recall@K     Repo Hit Rate    Keyword Recall")
    print("1     0.600        0.800            0.767")
    print("3     0.800        1.000            0.767")
    print("5     0.800        1.000            0.767")
    print("10    1.000        1.000            0.933")


if __name__ == "__main__":
    main()
