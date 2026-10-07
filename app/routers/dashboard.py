import json
import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.database import db
from app.dependencies import require_reader
from app.config import settings

logger = logging.getLogger(__name__)


def dashboard_rate_key(request: Request) -> str:
    # The API sees the gateway as its network peer. Keep one authenticated
    # browser session from consuming every other user's dashboard quota.
    cookie = request.cookies.get(settings.RADAR_SESSION_COOKIE)
    if cookie:
        return "session:" + hashlib.sha256(cookie.encode()).hexdigest()
    return "peer:" + get_remote_address(request)


limiter = Limiter(key_func=dashboard_rate_key)

router = APIRouter(
    prefix="/api/v1/dashboard",
    tags=["Dashboard"],
    dependencies=[Depends(require_reader)]
)

@router.get("/overview")
@limiter.limit("60/minute")
async def get_radar_overview(request: Request):
    """
    Return the current totals for signals, campaigns, and flagged accounts.
    """
    async with db.pool.acquire() as conn:
        try:
            stats = await conn.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM coordinated_signals) as total_signals,
                    (SELECT COUNT(DISTINCT text_hash) FROM coordinated_signals) as unique_campaigns,
                    (SELECT COUNT(*) FROM target_users WHERE trust_score < 50) as high_risk_users
            """)

            return {
                "status": "success",
                "metrics": {
                    "total_processed_signals": stats["total_signals"],
                    "active_manipulation_campaigns": stats["unique_campaigns"],
                    "flagged_high_risk_accounts": stats["high_risk_users"]
                }
            }
        except Exception:
            logger.exception("Dashboard overview query failed")
            raise HTTPException(status_code=500, detail="Unable to load dashboard overview")

@router.get("/active-campaigns")
@limiter.limit("30/minute")
async def get_active_campaigns(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 10):
    """
    List the largest coordinated signal groups by volume.
    """
    async with db.pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT
                    text_hash,
                    COUNT(tweet_id) as bot_tweet_count,
                    MIN(raw_data->>'text') as sample_tweet_text,
                    MAX(detected_at) as last_activity
                FROM coordinated_signals
                GROUP BY text_hash
                ORDER BY bot_tweet_count DESC
                LIMIT $1;
            """, limit)
            campaigns = []
            for row in rows:
                campaigns.append({
                    "campaign_id": row["text_hash"],
                    "impact_size": row["bot_tweet_count"],
                    "narrative_sample": row["sample_tweet_text"] or "N/A",
                    "last_seen_at": row["last_activity"].isoformat() if row["last_activity"] else None
                })

            return {"status": "success", "campaigns": campaigns}
        except Exception:
            logger.exception("Active campaigns query failed")
            raise HTTPException(status_code=500, detail="Unable to load active campaigns")

@router.get("/user-risk-map")
@limiter.limit("30/minute")
async def get_user_risk_map(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    """
    List accounts with a low trust score.
    """
    async with db.pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT
                    username,
                    trust_score,
                    raw_user_meta->>'followers_count' as followers,
                    last_analyzed
                FROM target_users
                WHERE trust_score < 60
                ORDER BY trust_score ASC, (raw_user_meta->>'followers_count')::int DESC
                LIMIT $1;
            """, limit)

            users = []
            for row in rows:
                users.append({
                    "username": row["username"],
                    "trust_score": row["trust_score"],
                    "followers": int(row["followers"]) if row["followers"] else 0,
                    "last_seen": row["last_analyzed"].isoformat() if row["last_analyzed"] else None
                })

            return {"status": "success", "suspects": users}
        except Exception:
            logger.exception("User risk map query failed")
            raise HTTPException(status_code=500, detail="Unable to load user risk map")


@router.get("/crypto-intelligence")
@limiter.limit("30/minute")
async def get_crypto_networks(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """Return detected wallet activity and associated accounts."""
    async with db.pool.acquire() as conn:
        try:
            records = await conn.fetch("""
                SELECT wallet_address, currency, associated_username, balance_usd, total_transactions, last_checked_at
                FROM crypto_intelligence
                WHERE balance_usd > 0 OR total_transactions > 0
                ORDER BY balance_usd DESC
                LIMIT $1;
            """, limit)

            return {
                "status": "success",
                "total_traced_wallets": len(records),
                "data": [dict(r) for r in records]
            }
        except Exception:
            logger.exception("Crypto intelligence query failed")
            raise HTTPException(status_code=500, detail="Unable to load crypto intelligence")


@router.get("/visual-threats")
@limiter.limit("30/minute")
async def get_visual_threats(
    request: Request,
    min_risk_score: int = 50,
    limit: Annotated[int, Query(ge=1, le=100)] = 15,
):
    """Return visual intelligence reports above the risk threshold."""
    async with db.pool.acquire() as conn:
        try:
            records = await conn.fetch("""
                SELECT tweet_id, image_url, has_weapons, has_narcotics, detected_objects, risk_score, analyzed_at
                FROM visual_intelligence
                WHERE risk_score >= $1
                ORDER BY risk_score DESC, analyzed_at DESC
                LIMIT $2;
            """, min_risk_score, limit)

            results = []
            for r in records:
                row_dict = dict(r)
                if isinstance(row_dict.get("detected_objects"), str):
                    row_dict["detected_objects"] = json.loads(row_dict["detected_objects"])
                results.append(row_dict)

            return {
                "status": "success",
                "critical_visual_threats": len(results),
                "data": results
            }
        except Exception:
            logger.exception("Visual intelligence query failed")
            raise HTTPException(status_code=500, detail="Unable to load visual intelligence")


@router.get("/enterprise-overview")
@limiter.limit("60/minute")
async def get_enterprise_overview(request: Request):
    """Return a summary of text, crypto, and visual intelligence."""
    async with db.pool.acquire() as conn:
        try:
            stats = await conn.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM cartel_embeddings) as total_signals,
                    (SELECT SUM(balance_usd) FROM crypto_intelligence) as total_illicit_funds_usd,
                    (SELECT COUNT(*) FROM visual_intelligence WHERE has_weapons = TRUE) as armed_threats
            """)

            return {
                "status": "success",
                "metrics": {
                    "total_analyzed_signals": stats["total_signals"] or 0,
                    "traced_illicit_funds_usd": float(stats["total_illicit_funds_usd"] or 0),
                    "armed_visual_threats_detected": stats["armed_threats"] or 0
                }
            }
        except Exception:
            logger.exception("Enterprise overview query failed")
            raise HTTPException(status_code=500, detail="Unable to load enterprise overview")


@router.get("/live-threat-feed")
@limiter.limit("120/minute")
async def get_live_threat_feed(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """Return the most recently detected high-confidence threats."""
    async with db.pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT
                    c.tweet_id,
                    c.username,
                    c.raw_text,
                    c.threat_category,
                    c.confidence_score,
                    e.admiralty_code,
                    e.pushed_to_siem,
                    c.detected_at
                FROM cartel_threat_signals c
                LEFT JOIN enterprise_intel_reports e ON c.tweet_id = e.tweet_id
                WHERE c.confidence_score >= 80
                ORDER BY c.detected_at DESC
                LIMIT $1;
            """, limit)

            return {
                "status": "success",
                "feed": [dict(r) for r in rows]
            }
        except Exception:
            logger.exception("Live threat feed query failed")
            raise HTTPException(status_code=500, detail="Unable to load live threat feed")


@router.get("/fleet-status")
@limiter.limit("60/minute")
async def get_fleet_status(request: Request):
    """Return the current X account and proxy fleet status."""
    async with db.pool.acquire() as conn:
        try:
            accounts = await conn.fetch("""
                SELECT
                    status,
                    COUNT(*) as total,
                    SUM(total_requests) as total_requests
                FROM x_account_fleet
                GROUP BY status;
            """)

            proxies = await conn.fetch("""
                SELECT
                    status,
                    COUNT(*) as total
                FROM proxy_fleet
                GROUP BY status;
            """)

            return {
                "status": "success",
                "accounts": {r["status"]: {"count": r["total"], "requests": r["total_requests"] or 0} for r in accounts},
                "proxies": {r["status"]: r["total"] for r in proxies}
            }
        except Exception:
            logger.exception("Fleet status query failed")
            raise HTTPException(status_code=500, detail="Unable to load fleet status")


@router.get("/admiralty-distribution")
@limiter.limit("60/minute")
async def get_admiralty_distribution(request: Request):
    """Return counts by Admiralty code for charts."""
    async with db.pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT admiralty_code, COUNT(*) as count
                FROM enterprise_intel_reports
                GROUP BY admiralty_code
                ORDER BY count DESC;
            """)

            return {
                "status": "success",
                "distribution": {r["admiralty_code"]: r["count"] for r in rows}
            }
        except Exception:
            logger.exception("Admiralty distribution query failed")
            raise HTTPException(status_code=500, detail="Unable to load Admiralty distribution")


@router.get("/geo-threat-map")
@limiter.limit("60/minute")
async def get_geo_threat_map(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 30):
    """Return threat counts by region."""
    async with db.pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT
                    COALESCE(NULLIF(location_context, 'Bilinmiyor'), 'Unknown') as region,
                    COUNT(*) as threat_count,
                    MAX(confidence_score) as max_confidence
                FROM cartel_threat_signals
                WHERE location_context IS NOT NULL
                GROUP BY location_context
                ORDER BY threat_count DESC
                LIMIT $1;
            """, limit)

            return {
                "status": "success",
                "geo_data": [
                    {"region": r["region"], "threats": r["threat_count"], "max_confidence": r["max_confidence"]}
                    for r in rows
                ]
            }
        except Exception:
            logger.exception("Geographic threat map query failed")
            raise HTTPException(status_code=500, detail="Unable to load geographic threat map")


@router.get("/lexicon-overview")
@limiter.limit("60/minute")
async def get_lexicon_overview(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    """Return review status and recent discoveries in the target lexicon."""
    async with db.pool.acquire() as conn:
        try:
            stats = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE review_status = 'PENDING') as pending_count,
                    COUNT(*) FILTER (WHERE review_status = 'APPROVED') as approved_count
                FROM target_lexicon;
            """)

            recent_discoveries = await conn.fetch("""
                SELECT id, term, category, confidence_score, review_status, discovered_at
                FROM target_lexicon
                ORDER BY discovered_at DESC
                LIMIT $1;
            """, limit)

            return {
                "status": "success",
                "metrics": {
                    "waiting_analyst_approval": stats["pending_count"] or 0,
                    "active_in_production": stats["approved_count"] or 0,
                },
                "recent_discoveries": [dict(r) for r in recent_discoveries]
            }
        except Exception:
            logger.exception("Lexicon overview query failed")
            raise HTTPException(status_code=500, detail="Unable to load lexicon overview")


@router.get("/siem-push-status")
@limiter.limit("60/minute")
async def get_siem_push_status(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """Return the current delivery status of intelligence reports."""
    async with db.pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT
                    report_id,
                    target_username,
                    admiralty_code,
                    confidence_score,
                    pushed_to_siem,
                    created_at
                FROM enterprise_intel_reports
                ORDER BY created_at DESC
                LIMIT $1;
            """, limit)

            pushed = await conn.fetchval(
                "SELECT COUNT(*) FROM enterprise_intel_reports WHERE pushed_to_siem = TRUE"
            )
            failed = await conn.fetchval(
                "SELECT COUNT(*) FROM enterprise_intel_reports WHERE pushed_to_siem = FALSE"
            )

            return {
                "status": "success",
                "summary": {"pushed": pushed, "failed": failed},
                "reports": [dict(r) for r in rows]
            }
        except Exception:
            logger.exception("SIEM delivery status query failed")
            raise HTTPException(status_code=500, detail="Unable to load SIEM delivery status")


@router.get("/threat-actor-dossiers")
@limiter.limit("60/minute")
async def get_threat_actor_dossiers(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """Return actor dossiers produced by the graph clustering engine."""
    async with db.pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT
                    dossier_id,
                    codename,
                    associated_accounts,
                    associated_wallets,
                    estimated_budget_usd,
                    risk_level,
                    operational_status,
                    generated_at
                FROM threat_actor_dossiers
                ORDER BY estimated_budget_usd DESC
                LIMIT $1;
            """, limit)

            return {
                "status": "success",
                "total_cells": len(rows),
                "dossiers": [dict(r) for r in rows]
            }
        except Exception:
            logger.exception("Threat actor dossier query failed")
            raise HTTPException(status_code=500, detail="Unable to load threat actor dossiers")


@router.get("/telegram-links")
@limiter.limit("60/minute")
async def get_discovered_telegram_links(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    """Return discovered Telegram invite links for analyst review."""
    async with db.pool.acquire() as conn:
        try:
            rows = await conn.fetch("""
                SELECT
                    invite_url,
                    source_platform,
                    source_context_id,
                    discovery_status,
                    found_at
                FROM discovered_telegram_links
                ORDER BY found_at DESC
                LIMIT $1;
            """, limit)

            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM discovered_telegram_links WHERE discovery_status = 'PENDING_ANALYSIS'"
            )
            approved = await conn.fetchval(
                "SELECT COUNT(*) FROM discovered_telegram_links WHERE discovery_status = 'APPROVED_TO_JOIN'"
            )

            return {
                "status": "success",
                "summary": {"pending_analysis": pending, "approved_to_join": approved},
                "links": [dict(r) for r in rows]
            }
        except Exception:
            logger.exception("Telegram links query failed")
            raise HTTPException(status_code=500, detail="Unable to load Telegram links")


@router.get("/inspector/{actor_id}")
@limiter.limit("120/minute")
async def get_actor_inspector(request: Request, actor_id: str):
    """
    Return identity, financial, operational, and scoring details for an actor.
    """
    async with db.pool.acquire() as conn:
        # Kimlik + Platform
        identity = await conn.fetchrow("""
            SELECT target_username as username, source_platform as platform,
                   ARRAY[]::text[] as aliases
            FROM enterprise_intel_reports
            WHERE target_username = $1
            ORDER BY created_at DESC
            LIMIT 1;
        """, actor_id)

        # Financial activity
        wallets = await conn.fetch("""
            SELECT wallet_address, currency, balance_usd
            FROM crypto_intelligence
            WHERE associated_username = $1
            ORDER BY balance_usd DESC
            LIMIT 5;
        """, actor_id)

        total_volume = await conn.fetchval("""
            SELECT COALESCE(SUM(balance_usd), 0)
            FROM crypto_intelligence
            WHERE associated_username = $1;
        """, actor_id)

        # Operational activity
        operational = await conn.fetchrow("""
            SELECT threat_category, detected_at as last_seen
            FROM cartel_threat_signals
            WHERE username = $1
            ORDER BY detected_at DESC
            LIMIT 1;
        """, actor_id)

        # Confidence scoring
        scoring = await conn.fetchrow("""
            SELECT admiralty_code, confidence_score,
                   CASE WHEN confidence_score >= 90 THEN 'HIGH'
                        WHEN confidence_score >= 60 THEN 'MEDIUM'
                        ELSE 'LOW' END as risk_level
            FROM enterprise_intel_reports
            WHERE target_username = $1
            ORDER BY confidence_score DESC
            LIMIT 1;
        """, actor_id)

        return {
            "status": "success",
            "actor_id": actor_id,
            "identity": {
                "username": identity["username"] if identity else actor_id,
                "platform": identity["platform"] if identity else "UNKNOWN",
                "aliases": identity["aliases"] if identity else [],
            },
            "financial": {
                "wallets": [dict(w) for w in wallets],
                "total_volume": total_volume or 0,
            },
            "operational": {
                "top_keywords": ["narcotics", "logistics", "firearms"],
                "threat_category": operational["threat_category"] if operational else "Unknown",
                "last_seen": operational["last_seen"].isoformat() if operational and operational["last_seen"] else None,
            },
            "scoring": {
                "admiralty_code": scoring["admiralty_code"] if scoring else "F6",
                "confidence": scoring["confidence_score"] if scoring else 0,
                "risk_level": scoring["risk_level"] if scoring else "UNKNOWN",
            },
        }


@router.get("/system-health")
@limiter.limit("60/minute")
async def get_system_health(request: Request):
    """Return Redis queue depth and database connectivity status."""
    from datetime import datetime
    try:
        redis_client = request.app.state.redis_client
        queue_len = await redis_client.llen("queue:raw_tweets")
    except Exception:
        queue_len = -1

    try:
        async with db.pool.acquire() as conn:
            db_ping = await conn.fetchval("SELECT 1")
    except Exception:
        db_ping = 0

    return {
        "status": "success",
        "health": {
            "redis_queue_length": queue_len,
            "database_connected": db_ping == 1,
            "api_status": "ONLINE",
            "timestamp": datetime.utcnow().isoformat()
        }
    }
