"""
app/intel/comint_engine.py
COMINT audio analysis preparation module.

Transcription and voiceprint integrations are not configured yet. The task
returns an explicit unavailable result without storing sample data.
"""

import logging
from app.database import db

logger = logging.getLogger("COMINT_Engine")


class AudioComintProcessor:
    def __init__(self):
        # Regional dialect vocabulary; terms remain in their source languages.
        self.dialect_dictionary = {
            "Northern_Mexican": ["morro", "plebe", "fierro", "machín"],
            "Colombian": ["parce", "chimba", "paisa", "berraco"],
            "Central_American": ["maje", "cipote", "chucho"],
            "Turkish_Kurdish": ["gabar", "ceylan", "xort"],
            "Turkish_Inner_Anatolia": ["topal", "kızılcahamam", "tulum"],
        }

    async def process_voice_signal(
        self, report_id: str, audio_file_path: str
    ) -> dict:
        """Return an explicit unavailable result until real transcription exists.

        This task can be queued manually, so a sample transcript must never be
        persisted as if it came from the supplied audio file.
        """
        logger.warning("COMINT transcription is not configured; report %s was not analyzed", report_id)
        return {
            "status": "unavailable",
            "reason": "Audio transcription is not configured",
        }

    async def match_voiceprint(
        self, voiceprint_id: str
    ) -> list[dict]:
        """
        Find other records associated with the supplied voiceprint.
        """
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT report_id, audio_hash, transcript_text,
                       detected_accent, speaker_voiceprint_id,
                       processed_at
                FROM comint_audio_profiles
                WHERE speaker_voiceprint_id = $1
                ORDER BY processed_at DESC
                LIMIT 20;
                """,
                voiceprint_id,
            )
            return [dict(r) for r in rows]
