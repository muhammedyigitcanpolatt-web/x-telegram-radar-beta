"""
app/intel/stylometry_engine.py
NLP stylometry and writing-style fingerprint matching

Compares writing features across aliases on X and other sources, including
sentence length, punctuation, slang density, and emoji use.

S(A, B) = (vec_A · vec_B) / (||vec_A|| * ||vec_B||)
"""

import re
import math
import logging
from app.database import db

logger = logging.getLogger("StylometryEngine")


class StylometryAttributionEngine:
    def __init__(self):
        self.punctuation_regex = re.compile(r"[.,!?;:*)]")
        # Broad emoji Unicode ranges.
        self.emoji_regex = re.compile(
            r"[\U00002600-\U000027BF]"
            r"|[\U0001F300-\U0001FADF]"
            r"|[\U0001F600-\U0001F64F]"
            r"|[\U0001F680-\U0001F6FF]"
            r"|[\U0001F900-\U0001F9FF]"
            r"|[\U00002600-\U000026FF]"
        )

    def extract_stylistic_vector(self, text: str) -> list[float]:
        """
        Extract a writing-style fingerprint vector from text.
        Vector: [avg_sentence_len, punc_density*100, emoji_ratio*100,
                 uppercase_ratio*100, slang_count]
        """
        total_chars = len(text) if len(text) > 0 else 1
        words = text.split()
        total_words = len(words) if len(words) > 0 else 1
        sentences = [s for s in re.split(r"[.!?]", text) if s.strip()]
        total_sentences = len(sentences) if len(sentences) > 0 else 1

        # Calculate the feature metrics.
        avg_sentence_len = total_words / total_sentences
        punc_count = len(self.punctuation_regex.findall(text))
        punc_density = punc_count / total_chars
        emoji_count = len(self.emoji_regex.findall(text))
        emoji_ratio = emoji_count / total_words
        uppercase_count = sum(1 for c in text if c.isupper())
        uppercase_ratio = uppercase_count / total_chars

        # Count selected Turkish-language threat-market slang terms.
        slang_keywords = [
            "torba", "hap", "ot", "kenevir", "kokain", "bonzai",
            "cc", "cvv", "dump", "klon", "kart patlatma",
            "hack", "ddos", "sorgu", "mernis", "vesika",
        ]
        text_lower = text.lower()
        slang_count = sum(1 for kw in slang_keywords if kw in text_lower)

        # Normalize the vector values.
        vector = [
            avg_sentence_len,
            punc_density * 100,
            emoji_ratio * 100,
            uppercase_ratio * 100,
            float(slang_count),
        ]
        return vector

    async def save_actor_fingerprint(self, username: str, combined_text: str):
        """
        Save or update an account's writing-style fingerprint.
        """
        v = self.extract_stylistic_vector(combined_text)
        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO actor_stylometry_fingerprints
                (username, avg_sentence_length, punctuation_density,
                 emoji_ratio, uppercase_ratio, slang_count, raw_vector)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (username) DO UPDATE
                SET avg_sentence_length = $2,
                    punctuation_density = $3,
                    emoji_ratio = $4,
                    uppercase_ratio = $5,
                    slang_count = $6,
                    raw_vector = $7;
                """,
                username,
                v[0],
                v[1],
                v[2],
                v[3],
                v[4],
                v,
            )
        logger.info(f"🖊️ Stored stylometry fingerprint for {username}")

    async def find_alias_matches(
        self, target_username: str, threshold: float = 0.90
    ) -> list[dict]:
        """
        Find likely aliases using cosine similarity of writing-style vectors.
        """
        async with db.pool.acquire() as conn:
            target = await conn.fetchrow(
                """
                SELECT raw_vector FROM actor_stylometry_fingerprints
                WHERE username = $1;
                """,
                target_username,
            )
            if not target or not target["raw_vector"]:
                return []

            v1 = target["raw_vector"]
            all_actors = await conn.fetch(
                """
                SELECT username, raw_vector
                FROM actor_stylometry_fingerprints
                WHERE username <> $1;
                """,
                target_username,
            )

            matches = []
            for actor in all_actors:
                v2 = actor["raw_vector"]
                if not v2 or len(v1) != len(v2):
                    continue

                # Calculate cosine similarity.
                dot_product = sum(a * b for a, b in zip(v1, v2))
                norm_a = math.sqrt(sum(a * a for a in v1))
                norm_b = math.sqrt(sum(b * b for b in v2))

                similarity = (
                    dot_product / (norm_a * norm_b)
                    if (norm_a * norm_b) > 0
                    else 0
                )

                if similarity >= threshold:
                    matches.append(
                        {
                            "matched_username": actor["username"],
                            "match_confidence": round(similarity * 100, 2),
                        }
                    )

            return sorted(
                matches, key=lambda x: x["match_confidence"], reverse=True
            )

    async def cross_platform_attribution(
        self, username: str
    ) -> dict | None:
        """
        Return likely aliases for an account across platforms.
        """
        matches = await self.find_alias_matches(username, threshold=0.85)
        if not matches:
            return None

        return {
            "primary_username": username,
            "alias_matches": matches,
            "attribution_confidence": max(
                m["match_confidence"] for m in matches
            ),
            "platforms_involved": list(
                set(
                    "TELEGRAM" if m["matched_username"].startswith("tg_") else "X"
                    for m in matches
                )
            ),
        }
