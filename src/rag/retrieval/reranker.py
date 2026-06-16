"""
Cross-encoder reranker using BAAI/bge-reranker-v2-m3.
Takes top-N candidates from hybrid search, scores each (query, chunk) pair,
returns top-K by relevance.
"""

from sentence_transformers import CrossEncoder

_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
_model: CrossEncoder | None = None


def _get_model() -> CrossEncoder:
    global _model
    if _model is None:
        # Model is pre-downloaded; load from local cache without phoning
        # home to HF (avoids corporate-proxy SSL failures at runtime).
        try:
            _model = CrossEncoder(_MODEL_NAME, max_length=512, local_files_only=True)
        except Exception:
            # Cache miss → fall back to normal download path.
            _model = CrossEncoder(_MODEL_NAME, max_length=512)
    return _model


def rerank(query: str, chunks: list[dict], top_k: int = 8) -> list[dict]:
    """
    Rerank a list of retrieved chunks using the cross-encoder.
    Each chunk must have a "text" key.
    Returns top_k chunks sorted by reranker score (highest first).
    """
    if not chunks:
        return []

    model = _get_model()

    pairs = [(query, chunk["text"]) for chunk in chunks]
    scores = model.predict(pairs)

    for chunk, score in zip(chunks, scores):
        chunk["rerank_score"] = float(score)

    ranked = sorted(chunks, key=lambda c: c["rerank_score"], reverse=True)
    return ranked[:top_k]
