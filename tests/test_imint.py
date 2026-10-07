import json
import hashlib

import pytest

from app.intel import imint_engine
from app.intel.vision import MAX_IMAGE_BYTES


@pytest.mark.asyncio
async def test_unknown_model_region_does_not_store_zero_coordinates(monkeypatch, tmp_path):
    image = tmp_path / "fixture.jpg"
    image.write_bytes(b"\xff\xd8\xfffixture")

    class Response:
        status_code = 200

        def json(self):
            return {"response": json.dumps({
                "suspected_region": "Unknown",
                "terrain_features": ["forest"],
                "confidence": 99,
            })}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    class ForbiddenPool:
        def acquire(self):
            raise AssertionError("Unknown region must not be persisted as (0, 0)")

    monkeypatch.setattr(imint_engine.httpx, "AsyncClient", Client)
    monkeypatch.setattr(imint_engine.db, "pool", ForbiddenPool())
    assert await imint_engine.ImageGeoInferenceEngine().extract_imint_coordinates(
        "fixture-report", str(image)
    ) == {}


@pytest.mark.asyncio
async def test_oversized_local_image_is_not_sent_to_model(monkeypatch, tmp_path):
    image = tmp_path / "large.jpg"
    image.write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))

    def unexpected_client(**_kwargs):
        raise AssertionError("Oversized image must not reach the model")

    monkeypatch.setattr(imint_engine.httpx, "AsyncClient", unexpected_client)
    assert await imint_engine.ImageGeoInferenceEngine().extract_imint_coordinates(
        "fixture-report", str(image)
    ) == {}


@pytest.mark.asyncio
async def test_image_fingerprint_covers_entire_file(monkeypatch, tmp_path):
    first_bytes = b"\xff\xd8\xff" + b"x" * 4100 + b"first"
    second_bytes = b"\xff\xd8\xff" + b"x" * 4100 + b"second"
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    first.write_bytes(first_bytes)
    second.write_bytes(second_bytes)

    class Response:
        status_code = 200

        def json(self):
            return {"response": json.dumps({
                "suspected_region": "Sinaloa_Mountains",
                "terrain_features": ["mountains"],
                "confidence": 80,
            })}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    fingerprints = []

    class Connection:
        async def execute(self, _sql, _report_id, image_hash, *_args):
            fingerprints.append(image_hash)

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    monkeypatch.setattr(imint_engine.httpx, "AsyncClient", Client)
    monkeypatch.setattr(imint_engine.db, "pool", Pool())
    engine = imint_engine.ImageGeoInferenceEngine()
    await engine.extract_imint_coordinates("report-one", str(first))
    await engine.extract_imint_coordinates("report-two", str(second))

    assert fingerprints == [
        hashlib.sha256(first_bytes).hexdigest(),
        hashlib.sha256(second_bytes).hexdigest(),
    ]
