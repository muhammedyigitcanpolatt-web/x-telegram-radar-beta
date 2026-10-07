# app/workers/celery_app.py
import asyncio
import json
import logging

from celery import Celery
from celery import signals
from kombu import Queue

from app.config import settings

logger = logging.getLogger("CeleryPipeline")

celery = Celery(
    "shortmox_enterprise",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Istanbul",
    enable_utc=True,
    worker_concurrency=4,
    task_default_queue="celery",
    task_queues=(Queue("celery"), Queue("light_ops"), Queue("heavy_ai")),
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Separate priority queues keep heavy AI work off the main ingestion path.
    task_routes={
        "tasks.light_ingest_sweep": {"queue": "light_ops"},
        "tasks.heavy_ai_inference": {"queue": "heavy_ai"},
        "tasks.evolve_lexicon": {"queue": "light_ops"},
        "tasks.archive_to_cold": {"queue": "light_ops"},
        "tasks.trace_crypto_hops": {"queue": "heavy_ai"},
        "tasks.compute_stylometry": {"queue": "light_ops"},
        "tasks.run_imint_analysis": {"queue": "heavy_ai"},
        "tasks.run_comint_analysis": {"queue": "light_ops"},
        "tasks.run_predictive_anomalies": {"queue": "light_ops"},
    },
    beat_schedule={
        "lexicon-evolution": {
            "task": "tasks.evolve_lexicon",
            "schedule": 21600.0,  # 6 saat
        },
        "x-telegram-link-seeder": {
            "task": "tasks.x_telegram_link_seeder",
            "schedule": 18000.0,  # 5 saat
        },
        "osint-telegram-link-seeder": {
            "task": "tasks.osint_telegram_link_seeder",
            "schedule": 14400.0,  # 4 saat
        },
        "telegram-channel-sweep": {
            "task": "tasks.telegram_channel_sweep",
            "schedule": 300.0,  # Every five minutes; collection checks its enabled flag.
        },
        "archive-to-cold-lake": {
            "task": "tasks.archive_to_cold",
            "schedule": 86400.0,  # 24 saat (gece)
        },
    },
)

_worker_loop = None
_worker_db_ready = False


def _database_manager():
    from app.database import db
    return db


def operations_paused() -> bool:
    import redis

    client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=2)
    try:
        return client.get("radar:operations:state") == "PAUSED"
    finally:
        client.close()


def run_task_coroutine(coroutine):
    """Run each task on one worker-process loop so asyncpg pools stay loop-bound."""
    global _worker_loop, _worker_db_ready
    if _worker_loop is not None:
        try:
            if not _worker_db_ready:
                _worker_loop.run_until_complete(_database_manager().connect())
                _worker_db_ready = True
            return _worker_loop.run_until_complete(coroutine)
        except BaseException:
            if asyncio.iscoroutine(coroutine):
                coroutine.close()
            raise

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    database = _database_manager()
    try:
        loop.run_until_complete(database.connect())
        return loop.run_until_complete(coroutine)
    except BaseException:
        if asyncio.iscoroutine(coroutine):
            coroutine.close()
        raise
    finally:
        loop.run_until_complete(database.disconnect())
        loop.close()
        asyncio.set_event_loop(None)


@signals.worker_process_init.connect
def initialize_worker_database(**_kwargs):
    global _worker_loop, _worker_db_ready
    _worker_db_ready = False
    _worker_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_worker_loop)


@signals.worker_process_shutdown.connect
def close_worker_database(**_kwargs):
    global _worker_loop, _worker_db_ready
    if _worker_loop is None:
        return
    try:
        if _worker_db_ready or _database_manager().pool is not None:
            _worker_loop.run_until_complete(_database_manager().disconnect())
    finally:
        _worker_loop.close()
        _worker_loop = None
        _worker_db_ready = False
        asyncio.set_event_loop(None)


@celery.task(name="tasks.light_ingest_sweep", bind=True, max_retries=3)
def light_ingest_sweep(self, target_query: str):
    """Fetch recent X posts through the configured scraping/API provider and ingest them."""
    from app.scrapers.collection import collect_x_query, collection_source_status

    readiness = collection_source_status("x")
    if readiness["status"] != "ready":
        return readiness
    if operations_paused():
        return {"status": "paused"}
    return run_task_coroutine(collect_x_query(target_query))


@celery.task(name="tasks.campaign_ingest_sweep", queue="light_ops")
def campaign_ingest_sweep(template: str):
    """Run a configured search template sequentially with the same collection gates."""
    from app.scrapers.collection import collect_campaign, collection_source_status

    readiness = collection_source_status("x")
    if readiness["status"] != "ready":
        return readiness
    if operations_paused():
        return {"status": "paused"}
    return run_task_coroutine(collect_campaign(template))


@celery.task(name="tasks.heavy_ai_inference", bind=True, max_retries=2)
def heavy_ai_inference(
    self, report_id: str, source_tweet_id: str, raw_text: str, media_url: str = None
):
    """
    Stage 2 (heavy AI):
    Run GPU-intensive Llama3/Llava work in the heavy_ai queue so ingestion
    is not blocked.
    """
    if operations_paused():
        return {"status": "paused"}

    from app.workers.analyzer import CartelIntelligence
    from app.workers.analyzer import VisualOSINTTracker
    from app.database import db
    from app.cron.archive_to_cold import ColdDataLakeArchiver

    async def run_heavy():
        if not source_tweet_id:
            raise ValueError("source_tweet_id is required; report ID parsing is unsafe")
        logger.info(f"🧠 [GPU INFERENCE] Started local LLM/VLM analysis for report {report_id}.")

        # LLM text analysis.
        cti_engine = CartelIntelligence()
        text_analysis = await cti_engine.analyze_cartel_signal(raw_text)

        visual_analysis = {}
        if media_url:
            vision_tracker = VisualOSINTTracker()
            visual_analysis = await vision_tracker.analyze_image(media_url)

        # Serialize the hot/cold decision with the archiver's copy/delete.
        # The lock stays held through the cold append and FINAL read-back.
        async with db.pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", ColdDataLakeArchiver.LOCK_ID)
            try:
                report_source = await conn.fetchval(
                    "SELECT tweet_id FROM enterprise_intel_reports WHERE report_id = $1",
                    report_id,
                )
                if report_source != source_tweet_id:
                    raise RuntimeError("AI enrichment report does not match the source signal")
                update_result = await conn.execute("""
                    UPDATE cartel_threat_signals
                    SET llm_analysis = $1, visual_analysis = $2, ai_enriched = TRUE
                    WHERE tweet_id = $3 AND raw_text = $4;
                """, json.dumps(text_analysis), json.dumps(visual_analysis),
                    source_tweet_id, raw_text)
                if update_result == "UPDATE 1":
                    return f"Report {report_id} was enriched with AI analysis."
                if update_result != "UPDATE 0":
                    raise RuntimeError(
                        f"AI enrichment expected one signal update, got {update_result}"
                    )
                if await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM cartel_threat_signals WHERE tweet_id = $1)",
                    source_tweet_id,
                ):
                    raise RuntimeError("AI enrichment hot signal text does not match task input")

                cold_version = await conn.fetchval(
                    "SELECT nextval('public.cold_write_version_seq'::regclass)"
                )
                signal_id = await ColdDataLakeArchiver().enrich_archived_signal(
                    source_tweet_id, raw_text, text_analysis, visual_analysis,
                    version=cold_version,
                )
                return f"Archived signal {signal_id} for report {report_id} was enriched with AI analysis."
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", ColdDataLakeArchiver.LOCK_ID)

    return run_task_coroutine(run_heavy())


@celery.task(name="tasks.evolve_lexicon", queue="light_ops")
def evolve_lexicon():
    """
    Celery Beat triggers this every six hours to discover new slang in
    confirmed A1/B1 threats.
    and adds them to the target_lexicon table.
    """
    if operations_paused():
        return {"status": "paused"}
    from app.workers.lexicon_mutator import LexiconEvolutionEngine
    async def run_evolution():
        engine = LexiconEvolutionEngine()
        await engine.discover_new_slang()
    return run_task_coroutine(run_evolution())


@celery.task(name="tasks.x_telegram_link_seeder", queue="light_ops")
def x_telegram_link_seeder():
    from app.intel.link_extractor import TelegramLinkHunter
    from app.scrapers.collection import collection_source_status
    from app.scrapers.x_seeder_tg_links import XTelegramLinkSeeder

    readiness = collection_source_status("x")
    if readiness["status"] != "ready":
        return readiness
    if operations_paused():
        return {"status": "paused"}

    async def run_seeder():
        records = await XTelegramLinkSeeder().scan_for_telegram_link_records()
        hunter = TelegramLinkHunter()
        stored = 0
        for record in records:
            stored += await hunter.sniff_and_store_links(
                record["url"], "X_TWITTER", record["source_id"]
            )
        return {"status": "completed", "found": len(records), "stored": stored}

    return run_task_coroutine(run_seeder())


@celery.task(name="tasks.osint_telegram_link_seeder", queue="light_ops")
def osint_telegram_link_seeder():
    from app.intel.link_extractor import TelegramLinkHunter
    from app.scrapers.collection import collection_source_status
    from app.scrapers.osint_seeder import TARGET_SOURCES, scan_osint_for_telegram_links

    readiness = collection_source_status("osint")
    if readiness["status"] != "ready":
        return readiness
    if operations_paused():
        return {"status": "paused"}

    async def run_seeder():
        links = await scan_osint_for_telegram_links()
        hunter = TelegramLinkHunter()
        stored = 0
        context = ",".join(TARGET_SOURCES)
        for link in links:
            stored += await hunter.sniff_and_store_links(
                link, "PUBLIC_OSINT", context
            )
        return {"status": "completed", "found": len(links), "stored": stored}

    return run_task_coroutine(run_seeder())


@celery.task(name="tasks.telegram_channel_sweep", queue="light_ops")
def telegram_channel_sweep():
    from app.scrapers.collection import collection_source_status
    from app.scrapers.telegram_worker import collect_configured_channels

    readiness = collection_source_status("telegram")
    if readiness["status"] != "ready":
        return readiness
    if operations_paused():
        return {"status": "paused"}
    return run_task_coroutine(collect_configured_channels())


@celery.task(name="tasks.persona_warming_cycle", queue="light_ops")
def persona_warming_cycle():
    return {
        "status": "disabled",
        "reason": "Account warming is intentionally not part of the collection workflow.",
    }


@celery.task(name="tasks.archive_to_cold", queue="light_ops")
def archive_to_cold():
    """
    Run nightly to move records older than 30 days from PostgreSQL into
    ClickHouse cold storage.
    """
    if operations_paused():
        return {"status": "paused"}
    from app.cron.archive_to_cold import ColdDataLakeArchiver
    async def run_archive():
        archiver = ColdDataLakeArchiver()
        await archiver.archive_migration_cycle()
    return run_task_coroutine(run_archive())


@celery.task(name="tasks.trace_crypto_hops", queue="heavy_ai")
def trace_crypto_hops(start_wallet: str, max_depth: int = 5):
    """
    Trace outgoing transfers from the specified wallet up to five hops.
    """
    if operations_paused():
        return {"status": "paused"}
    from app.intel.money_laundry_tracer import CryptoHopTracer
    async def run_trace():
        tracer = CryptoHopTracer()
        await tracer.trace_money_laundry_pipeline(start_wallet, 1, max_depth)
    return run_task_coroutine(run_trace())


@celery.task(name="tasks.compute_stylometry", queue="light_ops")
def compute_stylometry(username: str, combined_text: str):
    """
    Extract a writing-style fingerprint and match aliases.
    """
    if operations_paused():
        return {"status": "paused"}
    from app.intel.stylometry_engine import StylometryAttributionEngine
    async def run_compute():
        engine = StylometryAttributionEngine()
        await engine.save_actor_fingerprint(username, combined_text)
        matches = await engine.find_alias_matches(username, threshold=0.85)
        return {
            "username": username,
            "alias_matches": matches,
            "match_count": len(matches),
        }
    return run_task_coroutine(run_compute())


@celery.task(name="tasks.run_imint_analysis", queue="heavy_ai")
def run_imint_analysis(report_id: str, image_path: str):
    """
    Use local Llava on the RTX 4060 to infer geographic clues in an image.
    """
    if operations_paused():
        return {"status": "paused"}
    from app.intel.imint_engine import ImageGeoInferenceEngine
    async def run_imint():
        engine = ImageGeoInferenceEngine()
        result = await engine.extract_imint_coordinates(
            report_id, image_path
        )
        return result
    return run_task_coroutine(run_imint())


@celery.task(name="tasks.run_comint_analysis", queue="light_ops")
def run_comint_analysis(report_id: str, audio_file_path: str):
    """
    Return unavailable until a real audio transcription integration is configured.
    No transcript or voiceprint is synthesized from the filename.
    """
    if operations_paused():
        return {"status": "paused"}
    from app.intel.comint_engine import AudioComintProcessor
    async def run_comint():
        processor = AudioComintProcessor()
        result = await processor.process_voice_signal(
            report_id, audio_file_path
        )
        return result
    return run_task_coroutine(run_comint())


@celery.task(name="tasks.run_predictive_anomalies", queue="light_ops")
def run_predictive_anomalies(target_lexicon_category: str = "Narkotik"):
    """
    Explicitly unavailable until a calibrated and reviewed model is configured.
    Kept registered so an old manual invocation cannot persist false warnings.
    """
    from app.intel.prediction_engine import unavailable_result
    return unavailable_result()
