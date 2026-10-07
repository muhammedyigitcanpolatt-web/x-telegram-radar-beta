from app.config import settings
from app.scrapers import darkweb_stealth


def test_optional_tor_collector_never_checks_ip_without_configured_proxy(monkeypatch):
    monkeypatch.setattr(settings, "TOR_SOCKS_PROXY", None)
    monkeypatch.setattr(settings, "TOR_CONTROL_PORT", None)
    monkeypatch.setattr(settings, "TOR_CONTROL_PASSWORD", None)
    monkeypatch.setattr(
        darkweb_stealth.curl_requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("No direct HTTP request is allowed")
        ),
    )

    collector = darkweb_stealth.TorZeroLeakScraper()
    assert collector._assert_zero_leak() is False
    assert collector._rotate_tor_circuit() is False
