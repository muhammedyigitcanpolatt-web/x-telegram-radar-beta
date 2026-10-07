"""Atomic admission control for the durable ingest queue."""

INGEST_QUEUE_KEY = "queue:raw_tweets"
INGEST_QUEUE_CAPACITY = 100_000
INGEST_RETRY_AFTER_SECONDS = 5
MAX_INGEST_RECORD_BYTES = 64 * 1024

# Redis executes the length check and push together. A full queue rejects the
# new record without evicting any previously acknowledged work.
ENQUEUE_IF_CAPACITY_SCRIPT = """
local size = redis.call('LLEN', KEYS[1])
if size >= tonumber(ARGV[1]) then
    return -1
end
return redis.call('LPUSH', KEYS[1], ARGV[2])
"""


async def enqueue_if_capacity(redis_client, serialized_record: str) -> int:
    """Return the accepted queue length, or -1 when admission is refused."""
    if len(serialized_record.encode("utf-8")) > MAX_INGEST_RECORD_BYTES:
        raise ValueError("Ingest record exceeds the 64 KiB size limit")
    return int(await redis_client.eval(
        ENQUEUE_IF_CAPACITY_SCRIPT,
        1,
        INGEST_QUEUE_KEY,
        INGEST_QUEUE_CAPACITY,
        serialized_record,
    ))
