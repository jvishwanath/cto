from openai import OpenAI
from .config import LITELLM_URL, LITELLM_API_KEY, MODELS

client = OpenAI(base_url=LITELLM_URL, api_key=LITELLM_API_KEY)


def embed_texts(texts: list[str]) -> list[list[float]]:
    response = client.embeddings.create(
        model=MODELS["embed"],
        input=texts,
        dimensions=1024,
    )
    return [item.embedding for item in response.data]


def embed_query(query: str) -> list[float]:
    return embed_texts([query])[0]
