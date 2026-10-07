# app/intel/stix.py
import uuid
import datetime
import logging

import httpx

logger = logging.getLogger("STIX_Exporter")


class SIEMIntegrationEngine:
    def __init__(self, customer_webhook_url: str = None):
        # Splunk or SOAR webhook destination.
        self.webhook_url = customer_webhook_url or "http://127.0.0.1:8005/api/v1/mock-siem"

    def generate_stix_bundle(
        self, username: str, admiralty: str, confidence: int, evidence: dict
    ) -> dict:
        """Convert threat intelligence to STIX 2.1 JSON."""
        current_time = datetime.datetime.utcnow().isoformat() + "Z"

        # Threat actor.
        actor_id = f"threat-actor--{uuid.uuid4()}"
        actor = {
            "type": "threat-actor",
            "spec_version": "2.1",
            "id": actor_id,
            "created": current_time,
            "modified": current_time,
            "name": f"X Account: @{username}",
            "threat_actor_types": ["organized-crime", "criminal-enterprise"],
            "confidence": confidence
        }

        # Indicator, such as a crypto wallet or a text marker.
        indicator_id = f"indicator--{uuid.uuid4()}"
        indicator = {
            "type": "indicator",
            "spec_version": "2.1",
            "id": indicator_id,
            "created": current_time,
            "modified": current_time,
            "name": f"Cartel Communication / Financial Trail ({admiralty})",
            "pattern": f"[x-account:username = '{username}']",
            "pattern_type": "stix",
            "valid_from": current_time
        }

        # STIX Bundle (Paket)
        bundle_id = f"bundle--{uuid.uuid4()}"
        bundle = {
            "type": "bundle",
            "id": bundle_id,
            "objects": [actor, indicator],
            "extensions": {
                "extension-definition--custom-shortmox": {
                    "extension_type": "property-extension",
                    "admiralty_code": admiralty,
                    "evidence_summary": evidence
                }
            }
        }

        return bundle

    async def push_to_siem(self, bundle: dict, idempotency_key: str | None = None) -> bool:
        """Send the generated STIX bundle to a SIEM or SOAR endpoint."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
                response = await client.post(self.webhook_url, json=bundle, headers=headers)
                if response.status_code in (200, 201, 202):
                    logger.info(
                        f"🚀 [SIEM PUSH] STIX bundle sent successfully. ID: {bundle['id']}"
                    )
                    return True
            except Exception as e:
                logger.error(f"❌ [SIEM PUSH ERROR] Could not reach the destination: {e}")
        return False
