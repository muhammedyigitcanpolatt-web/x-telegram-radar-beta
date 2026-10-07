import asyncio
import json
from collections import deque

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.ingest_queue import (
    ENQUEUE_IF_CAPACITY_SCRIPT,
    INGEST_QUEUE_CAPACITY,
    INGEST_QUEUE_KEY,
    INGEST_RETRY_AFTER_SECONDS,
    MAX_INGEST_RECORD_BYTES,
)
from app.routers.ingest import router


class AtomicQueueRedis:
    """Model a Redis EVAL's indivisible execution, without an external server."""

    def __init__(self, records=()):
        self.records = deque(records)
        self.calls = []
        self.lock = asyncio.Lock()

    async def eval(self, script, key_count, key, capacity, record):
        assert script == ENQUEUE_IF_CAPACITY_SCRIPT
        assert key_count == 1
        assert key == INGEST_QUEUE_KEY
        assert capacity == INGEST_QUEUE_CAPACITY
        self.calls.append((key, record))
        # Requests may reach Redis concurrently, but each EVAL completes before
        # another can observe or change the same list.
        await asyncio.sleep(0)
        async with self.lock:
            if len(self.records) >= capacity:
                return -1
            self.records.appendleft(record)
            return len(self.records)


@pytest.fixture
def make_app(monkeypatch):
    monkeypatch.setattr(settings, "RADAR_INGEST_API_KEY", "backpressure-fixture-key")

    def factory(records=()):
        app = FastAPI()
        redis = AtomicQueueRedis(records)
        app.state.redis_client = redis
        app.include_router(router)
        return app, redis

    return factory


HEADERS = {"X-Radar-Ingest-Key": "backpressure-fixture-key"}


def test_100000th_record_is_accepted_and_100001st_is_rejected_without_eviction(make_app):
    existing = [f"previous-{index}" for index in range(99_999)]
    app, redis = make_app(existing)
    with TestClient(app) as client:
        accepted = client.post("/api/v1/ingest", json={"tweet_id": "new-100000"}, headers=HEADERS)
        assert accepted.status_code == 200
        assert accepted.json() == {"status": "queued", "queue_len": 100_000}
        before_rejection = list(redis.records)
        rejected = client.post("/api/v1/ingest", json={"tweet_id": "new-100001"}, headers=HEADERS)
    assert rejected.status_code == 503
    assert rejected.headers["Retry-After"] == str(INGEST_RETRY_AFTER_SECONDS)
    assert rejected.json() == {"detail": "Ingest queue is full; retry this record later."}
    assert list(redis.records) == before_rejection
    assert list(redis.records)[1:] == existing


@pytest.mark.parametrize("initial_size", [100_000, 100_001])
def test_full_or_preexisting_overfull_queue_remains_untouched(make_app, initial_size):
    existing = [f"accepted-{index}" for index in range(initial_size)]
    app, redis = make_app(existing)
    with TestClient(app) as client:
        response = client.post("/api/v1/ingest", json={"tweet_id": "rejected"}, headers=HEADERS)
    assert response.status_code == 503
    assert list(redis.records) == existing


@pytest.mark.asyncio
async def test_concurrent_requests_compete_for_one_remaining_slot(make_app):
    existing = [f"accepted-{index}" for index in range(99_999)]
    app, redis = make_app(existing)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*(
            client.post("/api/v1/ingest", json={"tweet_id": f"candidate-{index}"}, headers=HEADERS)
            for index in range(12)
        ))
    assert sum(response.status_code == 200 for response in responses) == 1
    assert sum(response.status_code == 503 for response in responses) == 11
    assert len(redis.records) == 100_000
    assert list(redis.records)[1:] == existing
    assert next(r for r in responses if r.status_code == 200).json()["queue_len"] == 100_000


def test_accepted_payload_keeps_canonical_source_identity_and_response_shape(make_app):
    app, redis = make_app()
    payload = {"source_platform": "telegram", "source_channel_id": "-10042", "tweet_id": "77", "text": "fixture"}
    with TestClient(app) as client:
        response = client.post("/api/v1/ingest", json=payload, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"status": "queued", "queue_len": 1}
    assert json.loads(redis.records[0]) == {
        **payload,
        "source_platform": "TELEGRAM",
        "tweet_id": "telegram:-10042:77",
        "source_message_id": "77",
    }


def test_invalid_payload_and_invalid_credentials_never_reach_queue(make_app):
    app, redis = make_app()
    with TestClient(app) as client:
        invalid_payload = client.post("/api/v1/ingest", json={"source_platform": "TELEGRAM"}, headers=HEADERS)
        invalid_credentials = client.post("/api/v1/ingest", json={"tweet_id": "77"}, headers={"X-Radar-Ingest-Key": "wrong"})
    assert invalid_payload.status_code == 422
    assert invalid_credentials.status_code == 403
    assert not redis.calls
    assert not redis.records


def test_oversized_utf8_record_is_rejected_before_queue_write(make_app):
    app, redis = make_app()
    # A character-count check would miss this payload because each glyph uses
    # two UTF-8 bytes; the Redis admission check must measure serialized bytes.
    payload = {"tweet_id": "oversized", "text": "é" * (MAX_INGEST_RECORD_BYTES // 2)}
    with TestClient(app) as client:
        response = client.post("/api/v1/ingest", json=payload, headers=HEADERS)

    assert response.status_code == 413
    assert response.json() == {"detail": "Ingest record exceeds the 64 KiB size limit"}
    assert redis.calls == []


def test_client_can_retry_after_consumer_frees_capacity(make_app):
    app, redis = make_app(["accepted"] * INGEST_QUEUE_CAPACITY)
    with TestClient(app) as client:
        first = client.post("/api/v1/ingest", json={"tweet_id": "retry-me"}, headers=HEADERS)
        assert first.status_code == 503
        redis.records.pop()
        retry = client.post("/api/v1/ingest", json={"tweet_id": "retry-me"}, headers=HEADERS)
    assert retry.status_code == 200
    assert retry.json() == {"status": "queued", "queue_len": INGEST_QUEUE_CAPACITY}
    assert json.loads(redis.records[0])["tweet_id"] == "retry-me"
