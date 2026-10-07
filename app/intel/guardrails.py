"""
app/intel/guardrails.py
LLM prompt injection and output validation guardrails

Filters known instruction patterns in external text and validates LLM output
against a strict JSON schema.
"""

import re
import json
import logging
from pydantic import BaseModel, ValidationError
from typing import List, Dict, Any

logger = logging.getLogger("GuardrailsEngine")


class StrictThreatSchema(BaseModel):
    is_threat: bool
    confidence_score: int
    admiralty_code: str
    extracted_entities: List[str]
    reasoning: str


class LLMGuardrailSystem:
    def __init__(self):
        # Common prompt injection patterns.
        self.injection_patterns = [
            re.compile(
                r"ignore\s+(?:previous|above)\s+instructions",
                re.IGNORECASE,
            ),
            re.compile(
                r"system\s+prompt\s+override", re.IGNORECASE
            ),
            re.compile(
                r"you\s+must\s+respond\s+as", re.IGNORECASE
            ),
            re.compile(
                r"admiralty\s+code\s+to\s+[a-f][1-6]",
                re.IGNORECASE,
            ),
            re.compile(
                r"f6\s+trustworthy", re.IGNORECASE
            ),
            re.compile(
                r"override\s+(?:scoring|confidence)", re.IGNORECASE
            ),
            re.compile(
                r"do\s+not\s+(?:analyze|flag|report)", re.IGNORECASE
            ),
        ]

    def sanitize_input_text(self, raw_input: str) -> str:
        """
        Detect and replace known prompt injection patterns in raw text.
        """
        cleaned_text = raw_input
        detected = False
        for pattern in self.injection_patterns:
            if pattern.search(raw_input):
                detected = True
                cleaned_text = pattern.sub(
                    "[MALICIOUS_INJECTION_BLOCKED]", cleaned_text
                )
        if detected:
            logger.critical(
                "🚨 [PROMPT INJECTION DETECTED] "
                "Potentially malicious input was blocked and masked."
            )
        return cleaned_text

    def validate_llm_json_output(
        self, raw_llm_response: str
    ) -> Dict[str, Any]:
        """
        Validate raw model output against the Pydantic schema and fall back
        to a conservative result if validation fails.
        """
        try:
            # Extract the JSON object from any surrounding Markdown.
            json_match = re.search(
                r"\{.*\}", raw_llm_response, re.DOTALL
            )
            if not json_match:
                raise ValueError(
                    "No valid JSON object was found in the response."
                )

            parsed_json = json.loads(json_match.group(0))

            # Validate the schema with Pydantic.
            validated_data = StrictThreatSchema(**parsed_json)
            return validated_data.model_dump()

        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            logger.error(
                f"❌ [AI OUTPUT CORRUPTION] "
                f"Model output failed validation: {e}"
            )
            # Conservative fallback.
            return {
                "is_threat": True,
                "confidence_score": 50,
                "admiralty_code": "F6",
                "extracted_entities": [],
                "reasoning": (
                    "System warning: Model output failed schema validation. "
                    "The output may be corrupted or adversarial."
                ),
            }

    def sanitize_and_validate_pipeline(
        self, raw_input: str, raw_llm_response: str
    ) -> Dict[str, Any]:
        """
        Sanitize input and validate output in one step.
        """
        _ = self.sanitize_input_text(raw_input)
        return self.validate_llm_json_output(raw_llm_response)
