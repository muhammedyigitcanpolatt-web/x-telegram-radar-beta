import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import AuthUser
from app.dependencies import require_admin
from app.workers.celery_app import celery
from app.intel.queries import list_available_templates
from app.scrapers.collection import collection_source_status, collection_status

logger = logging.getLogger("RadarOperations")
router = APIRouter(prefix="/api/v1/ops", tags=["Operations"])
OPERATIONS_STATE_KEY = "radar:operations:state"
PAUSED = "PAUSED"
RUNNING = "RUNNING"


@router.get("/templates", dependencies=[Depends(require_admin)])
async def list_campaign_templates():
    return {
        "status": "success",
        "templates": list_available_templates(),
        "collection": collection_status(),
    }


def _require_collection_source(source: str) -> None:
    readiness = collection_source_status(source)
    if readiness["status"] == "ready":
        return
    if readiness["status"] == "configuration_required":
        detail = f"{readiness['reason']} Missing settings: {', '.join(readiness['missing'])}."
    else:
        detail = readiness["reason"]
    raise HTTPException(status_code=503, detail=detail)


def _enqueue(task_name: str, *args):
    task = celery.tasks.get(task_name)
    if task is None:
        raise HTTPException(status_code=503, detail="Collection worker is unavailable.")
    try:
        result = task.apply_async(args=list(args), queue="light_ops")
    except Exception as exc:
        logger.exception("Could not enqueue collection task %s", task_name)
        raise HTTPException(
            status_code=503,
            detail="Collection worker queue is unavailable; no collection was started.",
        ) from exc
    return result


@router.post("/trigger-sweep", dependencies=[Depends(require_admin)])
async def trigger_intelligence_sweep(query: str = Query(..., min_length=1, max_length=512)):
    _require_collection_source("x")
    task = _enqueue("tasks.light_ingest_sweep", query)
    return {"status": "queued", "source": "x", "task_id": task.id}


@router.post("/trigger-campaign", dependencies=[Depends(require_admin)])
async def trigger_campaign_sweep(template: str):
    from app.intel.queries import CampaignTemplate

    try:
        campaign = CampaignTemplate(template)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Unknown campaign template.") from exc
    _require_collection_source("x")
    task = _enqueue("tasks.campaign_ingest_sweep", campaign.value)
    return {
        "status": "queued",
        "source": "x",
        "campaign": campaign.value,
        "task_id": task.id,
    }


@router.post("/trigger-telegram-sweep", dependencies=[Depends(require_admin)])
async def trigger_telegram_sweep():
    _require_collection_source("telegram")
    task = _enqueue("tasks.telegram_channel_sweep")
    return {"status": "queued", "source": "telegram", "task_id": task.id}


@router.post("/trigger-link-seeder", dependencies=[Depends(require_admin)])
async def trigger_link_seeder(source: Literal["x", "osint"] = Query(...)):
    _require_collection_source(source)
    task_name = (
        "tasks.x_telegram_link_seeder"
        if source == "x"
        else "tasks.osint_telegram_link_seeder"
    )
    task = _enqueue(task_name)
    return {"status": "queued", "source": source, "task_id": task.id}


@router.post("/kill-switch")
async def engage_kill_switch(
    request: Request, _user: AuthUser = Depends(require_admin)
):
    redis_client = request.app.state.redis_client
    await redis_client.set(OPERATIONS_STATE_KEY, PAUSED)

    control = celery.control
    report = {
        "state": PAUSED,
        "worker_acknowledged": False,
        "celery_workers_acknowledged": False,
        "analyzer_acknowledged": False,
        "analyzer_state": None,
        "workers": [],
        "queued_tasks_removed": None,
        "active_tasks_cancel_requested": 0,
        "active_tasks_remaining": None,
        "pending_ingest_records": None,
    }
    try:
        inspection = control.inspect(timeout=3.0)
        ping = await asyncio.to_thread(inspection.ping)
        report["workers"] = sorted((ping or {}).keys())
        active_by_worker = await asyncio.to_thread(inspection.active)
        reserved_by_worker = await asyncio.to_thread(inspection.reserved)
        active = [item for rows in (active_by_worker or {}).values() for item in rows]
        reserved = [item for rows in (reserved_by_worker or {}).values() for item in rows]
        task_ids = sorted({item["id"] for item in active + reserved if item.get("id")})
        for task_id in task_ids:
            # Billiard turns child SIGTERM into SystemExit. Celery's JSON result
            # backend cannot serialize that BaseException during active revoke.
            # An emergency stop must end the child without that failure path.
            control.revoke(task_id, terminate=True, signal="SIGKILL")
        report["active_tasks_cancel_requested"] = len(task_ids)

        # Celery declares the application queues explicitly; purge affects only
        # these queues and never deletes unrelated Redis keys.
        report["queued_tasks_removed"] = await asyncio.to_thread(control.purge)
        after = await asyncio.to_thread(inspection.active)
        reserved_after = await asyncio.to_thread(inspection.reserved)
        still_active = [item for rows in (after or {}).values() for item in rows]
        still_reserved = [item for rows in (reserved_after or {}).values() for item in rows]
        report["active_tasks_remaining"] = len(still_active)
        report["reserved_tasks_remaining"] = len(still_reserved)
        report["celery_workers_acknowledged"] = (
            bool(report["workers"]) and not still_active and not still_reserved
        )
    except Exception as exc:
        logger.exception("Kill switch could not confirm all worker actions")
        report["error"] = type(exc).__name__

    # The analyzer is a separate process from Celery. Wait briefly for its
    # heartbeat to confirm that it has observed the shared pause state.
    pause_deadline = asyncio.get_running_loop().time() + 3.0
    while asyncio.get_running_loop().time() < pause_deadline:
        report["analyzer_state"] = await redis_client.get("radar:analyzer:state")
        if report["analyzer_state"] == PAUSED:
            report["analyzer_acknowledged"] = True
            break
        await asyncio.sleep(0.1)

    try:
        report["pending_ingest_records"] = await redis_client.llen("queue:raw_tweets")
    except Exception:
        report["pending_ingest_records"] = None

    report["worker_acknowledged"] = (
        report["celery_workers_acknowledged"]
        and report["analyzer_acknowledged"]
    )
    report["status"] = "PAUSED" if report["worker_acknowledged"] else "PAUSE_REQUESTED"
    return report


@router.post("/resume")
async def resume_operations(
    request: Request, _user: AuthUser = Depends(require_admin)
):
    await request.app.state.redis_client.set(OPERATIONS_STATE_KEY, RUNNING)
    return {"status": "RESUME_REQUESTED", "state": RUNNING}
