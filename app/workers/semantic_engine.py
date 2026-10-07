import httpx
import logging
from app.database import db
from app.config import settings

logger = logging.getLogger("SemanticEngine")

class SemanticAnomalyDetector:
    def __init__(self):
        """
        Connect to local Ollama on the RTX 4060 host.
        """
        self.ollama_url = f"{settings.OLLAMA_URL}/api/embeddings"
        self.model = "nomic-embed-text"

    async def get_embedding(self, text: str) -> list[float] | None:
        """Convert text into a 768-dimensional embedding vector."""
        payload = {
            "model": self.model,
            "prompt": text
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(self.ollama_url, json=payload)
                if response.status_code == 200:
                    return response.json().get("embedding")
            except Exception as e:
                logger.error(f"Could not generate embedding (RTX 4060 / Ollama): {e}")
            return None

    async def find_semantic_coordination(self, target_embedding: list[float], threshold: float = 0.85) -> list:
        """
        Compare an incoming post's embedding with recent stored posts.
        Similar wording is not required when semantic similarity exceeds the threshold.
        organize operasyon (astroturfing) olarak yakalar.
        """
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    tweet_id,
                    text_hash,
                    (1 - (embedding <=> $1::vector)) as similarity
                FROM tweet_embeddings
                WHERE (1 - (embedding <=> $1::vector)) > $2
                ORDER BY similarity DESC
                LIMIT 10;
            """, target_embedding, threshold)
            return rows
