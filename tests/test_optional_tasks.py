from app.workers.celery_app import celery


def test_uncalibrated_prediction_returns_unavailable_without_writing(monkeypatch):
    from app.intel.prediction_engine import PredictiveTrendEngine
    from app.workers import celery_app

    monkeypatch.setattr(
        celery_app,
        "operations_paused",
        lambda: (_ for _ in ()).throw(AssertionError("Disabled task must not contact Redis")),
    )
    monkeypatch.setattr(
        celery_app,
        "run_task_coroutine",
        lambda _coroutine: (_ for _ in ()).throw(AssertionError("No DB task should start")),
    )

    result = celery_app.run_predictive_anomalies("Narkotik")
    assert result["status"] == "unavailable"
    assert "calibrated" in result["reason"]

    import asyncio
    assert asyncio.run(PredictiveTrendEngine().analyze_signal_anomalies("Narkotik")) == result
    assert asyncio.run(PredictiveTrendEngine().get_active_warnings()) == []


def test_uncalibrated_prediction_task_is_not_scheduled():
    assert "tasks.run_predictive_anomalies" in celery.tasks
    assert all(
        entry.get("task") != "tasks.run_predictive_anomalies"
        for entry in celery.conf.beat_schedule.values()
    )
