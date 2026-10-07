import pytest

from app.intel import comint_engine


@pytest.mark.asyncio
async def test_unconfigured_comint_does_not_persist_sample_intelligence(monkeypatch):
    class ForbiddenPool:
        def acquire(self):
            raise AssertionError("No COMINT record may be written without transcription")

    monkeypatch.setattr(comint_engine.db, "pool", ForbiddenPool())
    result = await comint_engine.AudioComintProcessor().process_voice_signal(
        "fixture-report", "/not/a/real/audio.wav"
    )

    assert result == {
        "status": "unavailable",
        "reason": "Audio transcription is not configured",
    }
    assert "transcript" not in result
    assert "voiceprint" not in result
