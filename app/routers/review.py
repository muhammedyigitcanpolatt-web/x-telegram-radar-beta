# app/routers/review.py
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.database import db
from app.auth import AuthUser
from app.config import settings
from app.dependencies import require_analyst, require_reader
from app.source_identity import normalize_source_record

logger = logging.getLogger("AnalystReview")

_SOURCE_IDENTITY_KEYS = (
    "tweet_id", "source_platform", "source_channel_id", "source_message_id"
)

router = APIRouter(
    prefix="/api/v1/review",
    tags=["Analyst Review Chamber"]
)


class LexiconReviewPayload(BaseModel):
    term_id: int
    decision: str  # APPROVED, REJECTED
    notes: Optional[str] = None


class AlertReviewPayload(BaseModel):
    report_id: str
    decision: str  # APPROVED, REJECTED


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _pending_alert_view(row):
    """Keep evidence available after CTI moves to cold storage."""
    alert = dict(row)
    raw_text = alert.pop("hot_raw_text")
    original_raw_json = alert.pop("original_raw_json")
    stix_payload = alert.pop("stix_payload")

    if not isinstance(raw_text, str):
        original = _json_object(original_raw_json)
        if original is not None:
            try:
                source = normalize_source_record(original)
            except ValueError:
                source = None
            if source is not None and all(
                source[key] == alert[key] for key in _SOURCE_IDENTITY_KEYS
            ):
                candidate = original.get("text")
                if isinstance(candidate, str):
                    raw_text = candidate

    if not isinstance(raw_text, str):
        report = _json_object(stix_payload)
        if report is not None:
            extensions = report.get("extensions")
            if isinstance(extensions, dict):
                extension = extensions.get("extension-definition--custom-shortmox")
                if isinstance(extension, dict):
                    evidence = extension.get("evidence_summary")
                    if isinstance(evidence, dict):
                        candidate = evidence.get("text")
                        if isinstance(candidate, str):
                            raw_text = candidate

    alert.pop("tweet_id")
    alert["raw_text"] = raw_text if isinstance(raw_text, str) else None
    return alert


async def _export_approved_report(report_id: str, analyst_username: str, action: str):
    """Claim a report, export it with a stable idempotency key, and record outcome."""
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            report = await conn.fetchrow("""
                SELECT stix_payload, pushed_to_siem, review_status,
                       siem_export_status, siem_export_attempted_at
                FROM enterprise_intel_reports
                WHERE report_id = $1 FOR UPDATE;
            """, report_id)
            if not report:
                raise HTTPException(status_code=404, detail="Report not found.")
            if report["review_status"] != "APPROVED":
                raise HTTPException(status_code=409, detail="Only approved reports can be exported.")
            if report["pushed_to_siem"] or report["siem_export_status"] == "SENT":
                return {"external_submission": "sent", "already_sent": True, "reason": None}
            if not settings.SIEM_EXPORT_ENABLED:
                return {"external_submission": "not_sent", "reason": "SIEM export is disabled"}

            attempted_at = report["siem_export_attempted_at"]
            if (
                report["siem_export_status"] == "SENDING"
                and attempted_at is not None
                and attempted_at > datetime.now(timezone.utc) - timedelta(minutes=5)
            ):
                return {"external_submission": "in_progress", "reason": "An export attempt is already running."}

            attempt_token = await conn.fetchval("""
                UPDATE enterprise_intel_reports
                SET siem_export_status = 'SENDING',
                    siem_export_attempted_at = NOW(), siem_export_error = NULL
                WHERE report_id = $1 AND review_status = 'APPROVED'
                RETURNING siem_export_attempted_at;
            """, report_id)
            await conn.execute("""
                INSERT INTO analyst_audit_logs (analyst_username, action_type, target_id)
                VALUES ($1, $2, $3);
            """, analyst_username, action, report_id)
            stix_payload = report["stix_payload"]

    try:
        stix_data = stix_payload if isinstance(stix_payload, dict) else json.loads(stix_payload)
        from app.intel.stix import SIEMIntegrationEngine
        sent = await SIEMIntegrationEngine(settings.SIEM_WEBHOOK_URL).push_to_siem(
            stix_data, idempotency_key=report_id
        )
    except Exception:
        logger.exception("SIEM export attempt failed for report %s", report_id)
        sent = False

    async with db.pool.acquire() as conn:
        updated = await conn.execute("""
            UPDATE enterprise_intel_reports
            SET pushed_to_siem = $2,
                siem_export_status = $3,
                siem_export_error = $4
            WHERE report_id = $1 AND review_status = 'APPROVED'
              AND siem_export_status = 'SENDING'
              AND siem_export_attempted_at = $5;
        """, report_id, sent, "SENT" if sent else "FAILED",
             None if sent else "SIEM did not confirm receipt", attempt_token)
    if updated == "UPDATE 0":
        return {
            "external_submission": "superseded",
            "reason": "A newer export attempt controls the report status.",
        }
    return {
        "external_submission": "sent" if sent else "failed",
        "reason": None if sent else "SIEM did not confirm receipt",
    }


@router.get("/pending-lexicon")
async def get_pending_lexicon(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    _user: AuthUser = Depends(require_reader),
):
    """List newly discovered terms awaiting analyst approval."""
    async with db.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, term, category, confidence_score, discovered_at
            FROM target_lexicon
            WHERE review_status = 'PENDING'
            ORDER BY confidence_score DESC LIMIT $1;
        """, limit)
        return {"status": "success", "data": [dict(r) for r in rows]}


@router.post("/lexicon")
async def review_lexicon_term(payload: LexiconReviewPayload, user: AuthUser = Depends(require_analyst)):
    """Approve or reject a proposed lexicon term."""
    if payload.decision not in ('APPROVED', 'REJECTED'):
        raise HTTPException(status_code=400, detail="Invalid review decision.")

    is_active = True if payload.decision == 'APPROVED' else False

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            term = await conn.fetchrow("""
                SELECT review_status FROM target_lexicon WHERE id = $1 FOR UPDATE;
            """, payload.term_id)
            if not term:
                raise HTTPException(status_code=404, detail="Term not found.")
            if term["review_status"] != "PENDING":
                raise HTTPException(status_code=409, detail="Term has already been reviewed.")

            await conn.execute("""
                UPDATE target_lexicon
                SET review_status = $1, is_active = $2, reviewed_by = $3, review_notes = $4, last_used_at = NOW()
                WHERE id = $5;
            """, payload.decision, is_active, user.username, payload.notes, payload.term_id)

            await conn.execute("""
                INSERT INTO analyst_audit_logs (analyst_username, action_type, target_id, notes)
                VALUES ($1, $2, $3, $4);
            """, user.username, f"LEXICON_{payload.decision}", str(payload.term_id), payload.notes)

    logger.info("[ANALYST REVIEW] %s term %s -> %s", user.username, payload.term_id, payload.decision)
    return {"status": "success", "message": f"Term {payload.decision.lower()} successfully."}


@router.post("/alert")
async def review_intelligence_alert(payload: AlertReviewPayload, user: AuthUser = Depends(require_analyst)):
    """Review a report and export it after approval when enabled."""
    if payload.decision not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=400, detail="Invalid review decision.")

    async with db.pool.acquire() as conn:
        async with conn.transaction():
            report = await conn.fetchrow("""
                SELECT review_status
                FROM enterprise_intel_reports
                WHERE report_id = $1 FOR UPDATE;
            """, payload.report_id)
            if not report:
                raise HTTPException(status_code=404, detail="Report not found.")
            if report["review_status"] != "PENDING":
                raise HTTPException(status_code=409, detail="Report has already been reviewed.")
            await conn.execute("""
                UPDATE enterprise_intel_reports
                SET review_status = $1, pushed_to_siem = FALSE, reviewed_by = $2, reviewed_at = NOW()
                WHERE report_id = $3;
            """, payload.decision, user.username, payload.report_id)
            await conn.execute("""
                INSERT INTO analyst_audit_logs (analyst_username, action_type, target_id)
                VALUES ($1, $2, $3);
            """, user.username, f"ALERT_{payload.decision}", payload.report_id)
    export_result = {"external_submission": "not_sent", "reason": None}
    if payload.decision == "APPROVED":
        export_result = await _export_approved_report(
            payload.report_id, user.username, "SIEM_EXPORT_AFTER_APPROVAL"
        )

    logger.info("[ANALYST REVIEW] %s report %s -> %s; SIEM=%s", user.username, payload.report_id, payload.decision, export_result["external_submission"])
    return {
        "status": payload.decision.lower(),
        **export_result,
    }


@router.post("/alert/{report_id}/export")
async def retry_siem_export(report_id: str, user: AuthUser = Depends(require_analyst)):
    """Retry external delivery for an already approved report."""
    return await _export_approved_report(report_id, user.username, "SIEM_EXPORT_RETRY")


@router.get("/pending-alerts")
async def get_pending_alerts(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    _user: AuthUser = Depends(require_reader),
):
    """List intelligence reports awaiting analyst approval."""
    async with db.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT e.report_id, e.tweet_id, e.target_username, e.source_platform,
                   e.source_channel_id, e.source_message_id,
                   c.raw_text AS hot_raw_text,
                   r.raw_json AS original_raw_json, e.stix_payload,
                   e.admiralty_code, e.confidence_score, e.threat_type, e.created_at
            FROM enterprise_intel_reports e
            LEFT JOIN cartel_threat_signals c
              ON c.tweet_id = e.tweet_id
             AND c.source_platform = e.source_platform
             AND c.source_channel_id = e.source_channel_id
             AND c.source_message_id = e.source_message_id
            LEFT JOIN raw_tweets r
              ON r.tweet_id = e.tweet_id
             AND r.source_platform = e.source_platform
             AND r.source_channel_id = e.source_channel_id
             AND r.source_message_id = e.source_message_id
            WHERE e.review_status = 'PENDING'
            ORDER BY e.confidence_score DESC
            LIMIT $1;
        """, limit)
        return {"status": "success", "data": [_pending_alert_view(row) for row in rows]}
