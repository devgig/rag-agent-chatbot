import asyncio
import logging
from typing import List

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
Instrumentator().instrument(app).expose(app)
model = None


EMBEDDING_MODEL = "all-MiniLM-L6-v2"


class EmbeddingRequest(BaseModel):
    input: str | List[str]
    model: str = EMBEDDING_MODEL


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: List[dict]
    model: str
    usage: dict


@app.on_event("startup")
async def load_model():
    global model
    logger.info(f"Loading sentence-transformers model: {EMBEDDING_MODEL}")
    model = SentenceTransformer(EMBEDDING_MODEL)
    logger.info(f"Model loaded successfully (dim={model.get_sentence_embedding_dimension()})")


@app.post("/v1/embeddings")
async def create_embedding(request: EmbeddingRequest):
    texts = [request.input] if isinstance(request.input, str) else request.input
    # sentence-transformers' encode() is CPU-bound and synchronous. Running it
    # directly in this async handler would block the event loop and serialize
    # all incoming requests, even though uvicorn has multiple workers. Hand it
    # off to the default thread pool so concurrent requests can interleave.
    embeddings_arr = await asyncio.to_thread(model.encode, texts)
    embeddings = embeddings_arr.tolist()

    return EmbeddingResponse(
        data=[
            {"object": "embedding", "embedding": emb, "index": i}
            for i, emb in enumerate(embeddings)
        ],
        model=request.model,
        usage={
            "prompt_tokens": sum(len(t.split()) for t in texts),
            "total_tokens": sum(len(t.split()) for t in texts),
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": model is not None}
