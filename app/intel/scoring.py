# app/intel/scoring.py
import logging

logger = logging.getLogger("AdmiraltyScoring")


class EnterpriseScoringEngine:
    def __init__(self):
        pass

    def calculate_admiralty_code(
        self,
        vector_score: float,
        has_crypto: bool,
        visual_risk: int,
        account_age_days: int | None
    ) -> tuple[str, int]:
        """
        Score raw evidence using the NATO Admiralty scheme.
        Return (admiralty_code, confidence_percent).
        """
        confidence = 0
        reliability_char = "F"  # F: Reliability cannot be assessed (default).
        credibility_num = 6     # 6: Credibility cannot be assessed (default).

        # 1. Source reliability (A-F).
        if account_age_days is not None:
            if account_age_days > 365 and vector_score > 0.90:
                reliability_char = "A"  # Completely reliable.
            elif account_age_days > 90 or vector_score > 0.85:
                reliability_char = "B"  # Usually reliable.
            elif account_age_days > 30:
                reliability_char = "C"  # Fairly reliable.
            else:
                reliability_char = "D"  # Not usually reliable (new or bot account).

        # 2. Information credibility (1-6) based on corroborating evidence.
        evidence_count = 0
        if vector_score > 0.80:
            evidence_count += 1
        if has_crypto:
            evidence_count += 1
        if visual_risk > 50:
            evidence_count += 2  # Visual evidence receives greater weight.

        if evidence_count >= 3:
            credibility_num = 1  # Confirmed by other sources.
            confidence = 95
        elif evidence_count == 2:
            credibility_num = 2  # Probably true.
            confidence = 75
        elif evidence_count == 1:
            credibility_num = 3  # Possibly true.
            confidence = 45
        else:
            credibility_num = 4  # Doubtful.
            confidence = 20

        admiralty_code = f"{reliability_char}{credibility_num}"
        logger.info(
            f"📊 [SCORING] Admiralty code: {admiralty_code} | Confidence: {confidence}%"
        )

        return admiralty_code, confidence
