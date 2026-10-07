import json
from fastapi import APIRouter, HTTPException, Request, Depends
from app.dependencies import require_ingest_access
from app.ingest_queue import enqueue_if_capacity, INGEST_RETRY_AFTER_SECONDS
from app.source_identity import normalize_source_record

router = APIRouter()

@router.post("/api/v1/ingest")
async def ingest_tweet(payload: dict, request: Request, _user=Depends(require_ingest_access)):
    """Accept a source record into the durable processing queue."""
    try:
        payload = normalize_source_record(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    redis_client = request.app.state.redis_client
    try:
        queue_len = await enqueue_if_capacity(
            redis_client, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    if queue_len < 0:
        raise HTTPException(
            status_code=503,
            detail="Ingest queue is full; retry this record later.",
            headers={"Retry-After": str(INGEST_RETRY_AFTER_SECONDS)},
        )
    return {"status": "queued", "queue_len": queue_len}
