import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import app.routers.review as review_module
from app.auth import AuthUser
from app.config import settings


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeConnection:
    def __init__(self, report):
        self.report = report
        self.audit = []

    def transaction(self):
        return FakeTransaction()

    async def fetchrow(self, _sql, _report_id):
        return dict(self.report) if self.report else None

    async def fetchval(self, sql, _report_id):
        assert "RETURNING siem_export_attempted_at" in sql
        self.report["siem_export_status"] = "SENDING"
        self.report["siem_export_attempted_at"] = datetime.now(timezone.utc)
        return self.report["siem_export_attempted_at"]

    async def execute(self, sql, *args):
        if "SET pushed_to_siem = $2" in sql:
            if (
                self.report["siem_export_status"] != "SENDING"
                or self.report["siem_export_attempted_at"] != args[4]
            ):
                return "UPDATE 0"
            self.report["pushed_to_siem"] = args[1]
            self.report["siem_export_status"] = args[2]
            self.report["siem_export_error"] = args[3]
            return "UPDATE 1"
        elif "INSERT INTO analyst_audit_logs" in sql:
            self.audit.append(args)


class FakePool:
    def __init__(self, report):
        self.connection = FakeConnection(report)

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


def pending_alert_row(**overrides):
    row = {
        "report_id": "report--archived",
        "tweet_id": "telegram:-100123:42",
        "target_username": "tg_fixture",
        "source_platform": "TELEGRAM",
        "source_channel_id": "-100123",
        "source_message_id": "42",
        "hot_raw_text": None,
        "original_raw_json": {
            "tweet_id": "telegram:-100123:42",
            "source_platform": "TELEGRAM",
            "source_channel_id": "-100123",
            "source_message_id": "42",
            "text": "Archived source evidence",
        },
        "stix_payload": None,
        "admiralty_code": "A1",
        "confidence_score": 90,
        "threat_type": "fixture",
        "created_at": datetime(2026, 1, 1),
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_pending_review_uses_verified_raw_evidence_after_hot_archive(monkeypatch):
    row = pending_alert_row()
    row["original_raw_json"] = json.dumps(row["original_raw_json"])

    class PendingConnection:
        async def fetch(self, sql, limit):
            assert limit == 20
            for table in ("c", "r"):
                for field in (
                    "tweet_id", "source_platform", "source_channel_id",
                    "source_message_id",
                ):
                    assert f"{table}.{field} = e.{field}" in sql
            assert "e.review_status = 'PENDING'" in sql
            return [row]

    class PendingPool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return PendingConnection()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(review_module.db, "pool", PendingPool())
    result = await review_module.get_pending_alerts(_user=AuthUser("reader-a", "reader"))

    assert result["data"][0]["raw_text"] == "Archived source evidence"
    assert "original_raw_json" not in result["data"][0]
    assert "stix_payload" not in result["data"][0]


def test_pending_review_rejects_unrelated_raw_evidence_and_uses_own_report():
    row = pending_alert_row(
        original_raw_json={
            "tweet_id": "telegram:-100999:42",
            "source_platform": "TELEGRAM",
            "source_channel_id": "-100999",
            "source_message_id": "42",
            "text": "Unrelated source evidence",
        },
        stix_payload={
            "extensions": {
                "extension-definition--custom-shortmox": {
                    "evidence_summary": {"text": "Report's own evidence"}
                }
            }
        },
    )

    alert = review_module._pending_alert_view(row)
    assert alert["raw_text"] == "Report's own evidence"
    assert review_module._pending_alert_view(
        pending_alert_row(original_raw_json=row["original_raw_json"])
    )["raw_text"] is None


def test_pending_review_keeps_hot_evidence_when_present():
    alert = review_module._pending_alert_view(
        pending_alert_row(hot_raw_text="Current hot evidence")
    )
    assert alert["raw_text"] == "Current hot evidence"


@pytest.fixture
def export_settings(monkeypatch):
    original = (settings.SIEM_EXPORT_ENABLED, settings.SIEM_WEBHOOK_URL)
    monkeypatch.setattr(settings, "SIEM_EXPORT_ENABLED", True)
    monkeypatch.setattr(settings, "SIEM_WEBHOOK_URL", "https://siem.fixture.invalid/events")
    yield
    settings.SIEM_EXPORT_ENABLED, settings.SIEM_WEBHOOK_URL = original


@pytest.mark.asyncio
async def test_approved_report_export_is_retryable_and_idempotent(monkeypatch, export_settings):
    report = {
        "stix_payload": {"type": "bundle", "id": "bundle--fixture"},
        "pushed_to_siem": False,
        "review_status": "APPROVED",
        "siem_export_status": "NOT_SENT",
        "siem_export_attempted_at": None,
    }
    pool = FakePool(report)
    monkeypatch.setattr(review_module.db, "pool", pool)
    calls = []

    class FakeSIEM:
        def __init__(self, url):
            assert url == settings.SIEM_WEBHOOK_URL

        async def push_to_siem(self, payload, idempotency_key=None):
            calls.append((payload, idempotency_key))
            return len(calls) == 2

    monkeypatch.setattr("app.intel.stix.SIEMIntegrationEngine", FakeSIEM)

    first = await review_module._export_approved_report("report-1", "analyst-a", "SIEM_EXPORT_RETRY")
    assert first["external_submission"] == "failed"
    assert report["siem_export_status"] == "FAILED"
    assert report["pushed_to_siem"] is False

    second = await review_module._export_approved_report("report-1", "analyst-a", "SIEM_EXPORT_RETRY")
    assert second["external_submission"] == "sent"
    assert report["siem_export_status"] == "SENT"
    assert report["pushed_to_siem"] is True
    assert calls == [
        ({"type": "bundle", "id": "bundle--fixture"}, "report-1"),
        ({"type": "bundle", "id": "bundle--fixture"}, "report-1"),
    ]

    third = await review_module._export_approved_report("report-1", "analyst-a", "SIEM_EXPORT_RETRY")
    assert third["already_sent"] is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_retry_requires_approved_report_and_respects_disabled_export(monkeypatch):
    pending = {
        "stix_payload": {}, "pushed_to_siem": False,
        "review_status": "PENDING", "siem_export_status": "NOT_SENT",
        "siem_export_attempted_at": None,
    }
    monkeypatch.setattr(review_module.db, "pool", FakePool(pending))
    monkeypatch.setattr(settings, "SIEM_EXPORT_ENABLED", False)

    with pytest.raises(HTTPException) as blocked:
        await review_module._export_approved_report("report-2", "analyst-a", "SIEM_EXPORT_RETRY")
    assert blocked.value.status_code == 409

    pending["review_status"] = "APPROVED"
    result = await review_module._export_approved_report("report-2", "analyst-a", "SIEM_EXPORT_RETRY")
    assert result["external_submission"] == "not_sent"
    assert pending["siem_export_status"] == "NOT_SENT"


@pytest.mark.asyncio
async def test_stale_export_completion_cannot_overwrite_newer_delivery(monkeypatch, export_settings):
    report = {
        "stix_payload": {"type": "bundle", "id": "bundle--fixture"},
        "pushed_to_siem": False,
        "review_status": "APPROVED",
        "siem_export_status": "NOT_SENT",
        "siem_export_attempted_at": None,
    }
    monkeypatch.setattr(review_module.db, "pool", FakePool(report))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    class FakeSIEM:
        def __init__(self, _url):
            pass

        async def push_to_siem(self, _payload, idempotency_key=None):
            nonlocal calls
            assert idempotency_key == "report-3"
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()
                return False
            return True

    monkeypatch.setattr("app.intel.stix.SIEMIntegrationEngine", FakeSIEM)
    old_attempt = asyncio.create_task(
        review_module._export_approved_report("report-3", "analyst-a", "SIEM_EXPORT_RETRY")
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    report["siem_export_attempted_at"] = datetime.now(timezone.utc) - timedelta(minutes=6)

    newer = await review_module._export_approved_report(
        "report-3", "analyst-a", "SIEM_EXPORT_RETRY"
    )
    assert newer["external_submission"] == "sent"
    assert report["siem_export_status"] == "SENT"

    release_first.set()
    stale = await old_attempt
    assert stale["external_submission"] == "superseded"
    assert report["siem_export_status"] == "SENT"
    assert report["pushed_to_siem"] is True


@pytest.mark.asyncio
async def test_lexicon_review_is_one_time_and_audited_once(monkeypatch):
    class LexiconConnection:
        def __init__(self):
            self.status = "PENDING"
            self.audit = []

        def transaction(self):
            return FakeTransaction()

        async def fetchrow(self, sql, term_id):
            assert "FOR UPDATE" in sql
            return {"review_status": self.status} if term_id == 7 else None

        async def execute(self, sql, *args):
            if "UPDATE target_lexicon" in sql:
                self.status = args[0]
            elif "INSERT INTO analyst_audit_logs" in sql:
                self.audit.append(args)

    class LexiconPool:
        def __init__(self):
            self.connection = LexiconConnection()

        def acquire(self):
            return self

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_args):
            return False

    pool = LexiconPool()
    monkeypatch.setattr(review_module.db, "pool", pool)
    analyst = AuthUser("analyst-a", "analyst")
    approved = review_module.LexiconReviewPayload(term_id=7, decision="APPROVED")

    result = await review_module.review_lexicon_term(approved, analyst)
    assert result["status"] == "success"
    assert pool.connection.status == "APPROVED"
    assert len(pool.connection.audit) == 1

    with pytest.raises(HTTPException) as duplicate:
        await review_module.review_lexicon_term(approved, analyst)
    assert duplicate.value.status_code == 409
    assert len(pool.connection.audit) == 1

    with pytest.raises(HTTPException) as missing:
        await review_module.review_lexicon_term(
            review_module.LexiconReviewPayload(term_id=8, decision="APPROVED"), analyst
        )
    assert missing.value.status_code == 404
