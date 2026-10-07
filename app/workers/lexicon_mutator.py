# app/workers/lexicon_mutator.py
import json
import logging

import httpx

from app.config import settings
from app.database import db

logger = logging.getLogger("LexiconMutator")


class LexiconEvolutionEngine:
    def __init__(self):
        self.ollama_url = f"{settings.OLLAMA_URL}/api/generate"
        self.model = "llama3:8b"

    async def discover_new_slang(self):
        """Discover new code words in confirmed high-risk posts."""
        async with db.pool.acquire() as conn:
            samples = await conn.fetch("""
                SELECT raw_text FROM cartel_threat_signals
                WHERE confidence_score >= 80 ORDER BY detected_at DESC LIMIT 50
            """)

            if not samples:
                logger.info("[LEXICON] No qualifying high-confidence signals were found.")
                return

            combined_text = "\n---\n".join([s["raw_text"] for s in samples])

            prompt = f"""
            You are a senior narcotics intelligence analyst. Review the following
            high-confidence threat posts. Identify NEW code words, slang, or emoji
            combinations used for drugs, weapons, or illicit logistics.

            Posts:
            {combined_text}

            Respond ONLY in this JSON format:
            {{
                "new_terms": [
                    {{"term": "word", "category": "Narcotics/Weapons/Logistics", "explanation": "why this term was selected"}}
                ]
            }}
            """

            payload = {
                "model": self.model,
                "prompt": prompt,
                "format": "json",
                "stream": False
            }

            async with httpx.AsyncClient(timeout=60.0) as client:
                try:
                    resp = await client.post(self.ollama_url, json=payload)
                    if resp.status_code == 200:
                        discovered = json.loads(resp.json().get("response", "{}"))
                        await self._inject_new_terms(discovered.get("new_terms", []))
                except Exception as e:
                    logger.error(f"Lexicon mutation failed: {e}")

    async def _inject_new_terms(self, terms: list):
        """Insert new terms into the database unless they already exist."""
        async with db.pool.acquire() as conn:
            for item in terms:
                try:
                    await conn.execute("""
                        INSERT INTO target_lexicon (term, category, source, confidence_score)
                        VALUES ($1, $2, 'AI_DISCOVERED', 50)
                        ON CONFLICT (term) DO NOTHING
                    """, item["term"], item["category"])
                    logger.info(
                        f"✨ [NEW LEXICON] Discovered term: {item['term']} ({item['category']})"
                    )
                except Exception as e:
                    logger.debug(f"Could not insert term: {e}")
