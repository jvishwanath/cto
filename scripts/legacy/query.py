"""
Phase 0: Simple RAG query — embed question, retrieve top-k from Qdrant, stuff into LLM.
Usage: python scripts/query.py "how does JWT validation work?"
"""

import sys
from pathlib import Path

from openai import OpenAI
from qdrant_client import QdrantClient

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
from rag.config import QDRANT_URL, LITELLM_URL, LITELLM_API_KEY, MODELS
from rag.embed import embed_query

COLLECTION = "code"
TOP_K = 5

llm_client = OpenAI(base_url=LITELLM_URL, api_key=LITELLM_API_KEY)
qdrant = QdrantClient(url=QDRANT_URL)


def retrieve(query: str, top_k: int = TOP_K) -> list[dict]:
    vector = embed_query(query)
    results = qdrant.query_points(
        collection_name=COLLECTION,
        query=vector,
        limit=top_k,
        with_payload=True,
    )
    return [
        {
            "text": hit.payload["text"],
            "repo": hit.payload["repo"],
            "filepath": hit.payload["filepath"],
            "start_line": hit.payload["start_line"],
            "score": hit.score,
        }
        for hit in results.points
    ]


def generate_answer(query: str, chunks: list[dict]) -> str:
    context_parts = []
    for i, chunk in enumerate(chunks):
        label = f"{chunk['repo']}/{chunk['filepath']}:{chunk['start_line']}"
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
            {
                "role": "user",
                "content": f"Context:\n{context}\n\nQuestion: {query}",
            },
        ],
    )
    return response.choices[0].message.content


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/query.py \"your question here\"")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    print(f"Query: {query}\n")

    print("Retrieving...")
    chunks = retrieve(query)

    print(f"Top {len(chunks)} chunks:")
    for i, chunk in enumerate(chunks):
        print(f"  [{i+1}] {chunk['repo']}/{chunk['filepath']}:{chunk['start_line']} (score={chunk['score']:.3f})")
    print()

    print("Generating answer...\n")
    answer = generate_answer(query, chunks)
    print(answer)


if __name__ == "__main__":
    main()
