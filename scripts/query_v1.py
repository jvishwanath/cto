"""
Phase 1/3.5: Hybrid RAG query — dense + native BM25 with RRF fusion + reranker.
Usage: python scripts/query_v1.py "how does JWT validation work?"
"""

import sys
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from rag.config import LITELLM_URL, LITELLM_API_KEY, MODELS
from rag.retrieval import hybrid_search, rerank

RETRIEVE_K = 30
RERANK_TOP_K = 8

llm_client = OpenAI(base_url=LITELLM_URL, api_key=LITELLM_API_KEY)


def generate_answer(query: str, chunks: list[dict]) -> str:
    context_parts = []
    for i, chunk in enumerate(chunks):
        label = f"{chunk['repo']}/{chunk['filepath']}:{chunk['start_line']}"
        if chunk.get("symbol_name"):
            label += f" ({chunk['symbol_name']})"
        context_parts.append(f"[{i+1}] ({label}, score={chunk['score']:.3f})\n{chunk['text']}")

    context = "\n\n---\n\n".join(context_parts)

    response = llm_client.chat.completions.create(
        model=MODELS["agent"],
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a developer assistant. Answer the question using ONLY the "
                    "provided code context. Cite sources as [1], [2], etc. "
                    "If the context is insufficient, say so."
                ),
            },
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"},
        ],
    )
    return response.choices[0].message.content


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/query_v1.py \"your question here\"")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    print(f"Query: {query}\n")

    print("Retrieving (hybrid: dense + native BM25 + RRF)...")
    candidates = hybrid_search(query, top_k=RETRIEVE_K)
    print(f"  Retrieved {len(candidates)} candidates")

    print("Reranking (bge-reranker-v2-m3)...")
    chunks = rerank(query, candidates, top_k=RERANK_TOP_K)

    print(f"\nTop {len(chunks)} chunks (after reranking):")
    for i, chunk in enumerate(chunks):
        sym = f" [{chunk['symbol_name']}]" if chunk.get("symbol_name") else ""
        print(f"  [{i+1}] {chunk['repo']}/{chunk['filepath']}:{chunk['start_line']}{sym} "
              f"({chunk['chunk_type']}, rerank={chunk['rerank_score']:.3f})")
    print()

    print("Generating answer...\n")
    print(generate_answer(query, chunks))


if __name__ == "__main__":
    main()
