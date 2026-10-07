import pytest

from app.source_identity import normalize_source_record


def test_telegram_identity_uses_channel_and_message_ids():
    first = normalize_source_record({
        "source_platform": "telegram",
        "source_channel_id": -100123,
        "source_message_id": 42,
        "text": "fixture",
    })
    second = normalize_source_record({
        "source_platform": "TELEGRAM",
        "source_channel_id": "-100987",
        "source_message_id": "42",
        "text": "fixture",
    })

    assert first["tweet_id"] == "telegram:-100123:42"
    assert second["tweet_id"] == "telegram:-100987:42"
    assert first["tweet_id"] != second["tweet_id"]
    assert first["source_message_id"] == "42"


def test_telegram_canonical_id_is_idempotent_and_recovers_channel():
    record = normalize_source_record({
        "source_platform": "TELEGRAM",
        "tweet_id": "telegram:-100123:42",
        "text": "fixture",
    })

    assert record["tweet_id"] == "telegram:-100123:42"
    assert record["source_channel_id"] == "-100123"
    assert record["source_message_id"] == "42"


@pytest.mark.parametrize("record", [
    {"source_platform": "TELEGRAM", "source_message_id": "42"},
    {"source_platform": "TELEGRAM", "source_channel_id": "-1001"},
    {"source_platform": "X_TWITTER"},
])
def test_source_identity_rejects_missing_components(record):
    with pytest.raises(ValueError):
        normalize_source_record(record)
