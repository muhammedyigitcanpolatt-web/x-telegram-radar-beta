"""
app/intel/imint_engine.py
Experimental IMINT image geolocation module.

Coordinates are stored only when the local model selects a region from the
limited reference matrix. Unknown regions do not produce a location record.
"""

import os
import json
import logging
import base64
import httpx
from app.config import settings
from app.database import db
from app.intel.vision import MAX_IMAGE_BYTES

logger = logging.getLogger("IMINT_Engine")


class ImageGeoInferenceEngine:
    def __init__(self):
        self.ollama_url = f"{settings.OLLAMA_URL}/api/generate"
        # Limited reference matrix for terrain and regional landmarks.
        self.geo_matrix = {
            "Sinaloa_Mountains": {"lat": 25.0, "lng": -107.5},
            "Jalisco_Arid_Zone": {"lat": 20.6, "lng": -103.3},
            "Michoacan_Forest": {"lat": 19.5, "lng": -101.6},
            "Turkish_Cilicia": {"lat": 36.8, "lng": 35.0},
            "Turkish_East_Anatolia": {"lat": 39.0, "lng": 41.0},
        }

    async def extract_imint_coordinates(
        self, report_id: str, image_path: str
    ) -> dict:
        """
        Infer geographic clues from an image with local Llava and assign
        coordinates only for a supported region.
        """
        if not os.path.exists(image_path):
            logger.error(
                f"❌ [IMINT ERROR] Image was not found: "
                f"{image_path}"
            )
            return {}

        logger.warning(
            f"📸 [IMINT IMAGERY ANALYSIS] Analyzing image: "
            f"{image_path}"
        )

        try:
            with open(image_path, "rb") as image_file:
                image_bytes = image_file.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            logger.warning("IMINT image could not be read")
            return {}
        if len(image_bytes) > MAX_IMAGE_BYTES:
            logger.warning("IMINT image exceeds the configured size limit")
            return {}
        encoded_image = base64.b64encode(image_bytes).decode("utf-8")

        prompt = (
            "Analyze this image for tactical military intelligence. "
            "Identify the terrain (e.g., desert, pine forest, urban), "
            "specific vegetation (e.g., cacti types, agricultural crops), "
            "and any visible infrastructure (e.g., utility poles, "
            "highway signs). "
            "Respond ONLY in JSON: "
            '{"terrain_features":["feature1","feature2"],'
            '"suspected_region":"Sinaloa_Mountains/Unknown",'
            '"confidence":85.0}'
        )

        payload = {
            "model": "llava:latest",
            "prompt": prompt,
            "images": [encoded_image],
            "format": "json",
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                resp = await client.post(self.ollama_url, json=payload)
                if resp.status_code == 200:
                    raw_response = resp.json().get("response", "{}")
                    try:
                        analysis = json.loads(raw_response)
                    except json.JSONDecodeError:
                        logger.error(
                            f"IMINT JSON parse error: {raw_response[:200]}"
                        )
                        return {}

                    region = analysis.get("suspected_region", "Unknown")
                    coords = self.geo_matrix.get(region) if isinstance(region, str) else None
                    if coords is None:
                        logger.warning("IMINT model returned an unsupported region; no coordinates stored")
                        return {}
                    features = analysis.get("terrain_features", [])
                    confidence = analysis.get("confidence", 50.0)

                    # Compute a stable image fingerprint.
                    import hashlib
                    image_hash = hashlib.sha256(image_bytes).hexdigest()

                    async with db.pool.acquire() as conn:
                        await conn.execute(
                            """
                            INSERT INTO imint_geo_analysis
                            (report_id, image_hash,
                             detected_terrain_features, predicted_state,
                             predicted_latitude, predicted_longitude,
                             confidence_score)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT (image_hash) DO NOTHING;
                            """,
                            report_id,
                            image_hash,
                            features,
                            region,
                            coords["lat"],
                            coords["lng"],
                            confidence,
                        )

                    logger.info(
                        f"🎯 [IMINT MATCH] Suspected region: {region} "
                        f"(confidence: {confidence}%)"
                    )
                    return {
                        "latitude": coords["lat"],
                        "longitude": coords["lng"],
                        "region": region,
                        "confidence": confidence,
                        "features": features,
                    }
            except Exception as e:
                logger.error(f"IMINT geolocation failed: {e}")

        return {}
