"""Placeholder for future trend analysis.

The former implementation treated a Z-score as a calibrated probability of
real-world harm, used a category label without filtering by category, and
wrote the result as an active warning. Until a validated baseline and review
workflow exist, callers receive an explicit unavailable result instead.
Historical rows are left untouched for a separate data review.
"""

import logging

logger = logging.getLogger("PredictiveAnalytics")


def unavailable_result() -> dict[str, str]:
    return {
        "status": "unavailable",
        "reason": "A calibrated baseline and reviewed warning model are not configured",
    }


class PredictiveTrendEngine:
    async def analyze_signal_anomalies(
        self, target_lexicon_category: str = "Narkotik"
    ) -> dict[str, str]:
        logger.warning(
            "Predictive trend analysis is unavailable; category=%s",
            target_lexicon_category,
        )
        return unavailable_result()

    async def get_active_warnings(self, limit: int = 20) -> list[dict]:
        """Do not surface legacy, uncalibrated rows as active warnings."""
        return []
