"""
Phase 0: Eval harness — measures retrieval quality against golden set.
Usage: python eval/run_eval.py
"""

import json
import sys
from pathlib import Path

from qdrant_client import QdrantClient

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from rag.config import QDRANT_URL
from rag.embed import embed_query

COLLECTION = "code"
GOLDEN_SET = Path(__file__).parent / "golden_set.jsonl"
K_VALUES = [1, 3, 5, 10]


def load_golden_set() -> list[dict]:
    items = []
    with open(GOLDEN_SET) as f:
        for line in f:
            if line.strip():
                items.append(json.loads(line))
    return items


def evaluate_retrieval(qdrant: QdrantClient, item: dict, k: int) -> dict:
    vector = embed_query(item["question"])
    results = qdrant.query_points(
        collection_name=COLLECTION,
        query=vector,
        limit=k,
        with_payload=True,
    )

    retrieved_texts = []
    retrieved_repos = []
    for hit in results.points:
        retrieved_texts.append(hit.payload["text"].lower())
        retrieved_repos.append(hit.payload["repo"])

    # Check if expected repo appears in results
    repo_hit = any(item["expected_file"] in repo for repo in retrieved_repos)

    # Check if any expected keywords appear in retrieved chunks
    keywords = item.get("expected_keywords", [])
    keyword_hits = 0
    for kw in keywords:
        if any(kw.lower() in text for text in retrieved_texts):
            keyword_hits += 1

    keyword_recall = keyword_hits / len(keywords) if keywords else 0

    return {
        "repo_hit": repo_hit,
        "keyword_recall": keyword_recall,
        "hit": repo_hit and keyword_recall >= 0.5,
    }


def main():
    golden = load_golden_set()
    if not golden:
        print("No eval questions found in golden_set.jsonl")
        sys.exit(1)

    qdrant = QdrantClient(url=QDRANT_URL)

    if not qdrant.collection_exists(COLLECTION):
        print(f"Collection '{COLLECTION}' does not exist. Run index_repos.py first.")
        sys.exit(1)

    print(f"Running eval on {len(golden)} questions\n")
    print(f"{'K':<5} {'Recall@K':<12} {'Repo Hit Rate':<16} {'Keyword Recall'}")
    print("-" * 50)

    for k in K_VALUES:
        hits = 0
        repo_hits = 0
        total_keyword_recall = 0

        for item in golden:
            result = evaluate_retrieval(qdrant, item, k)
            if result["hit"]:
                hits += 1
            if result["repo_hit"]:
                repo_hits += 1
            total_keyword_recall += result["keyword_recall"]

        n = len(golden)
        print(
            f"{k:<5} {hits/n:<12.3f} {repo_hits/n:<16.3f} "
            f"{total_keyword_recall/n:.3f}"
        )

    # Detail per question at k=5
    print(f"\n\nDetail (k=5):")
    print(f"{'Question':<55} {'Hit':<6} {'KW Recall'}")
    print("-" * 75)
    for item in golden:
        result = evaluate_retrieval(qdrant, item, 5)
        q = item["question"][:52] + "..." if len(item["question"]) > 55 else item["question"]
        print(f"{q:<55} {'✓' if result['hit'] else '✗':<6} {result['keyword_recall']:.2f}")


if __name__ == "__main__":
    main()
