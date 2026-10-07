import base64
import socket

import httpx
import pytest

from app.intel import vision


class MemoryStream(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


def mock_image_client(monkeypatch, handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(vision.httpx, "AsyncClient", client_factory)


def mock_dns(monkeypatch, answers):
    lookups = []

    async def lookup(host, port):
        lookups.append((host, port))
        return answers[host]

    monkeypatch.setattr(vision, "_lookup_addresses", lookup)
    return lookups


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://media.example/image.jpg",
    "https://user:pass@media.example/image.jpg",
    "https://@media.example/image.jpg",
    "http://127.0.0.1/image.jpg",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.3/image.jpg",
    "http://100.64.0.1/image.jpg",
    "http://192.0.2.1/image.jpg",
    "http://224.0.0.1/image.jpg",
    "http://[::1]/image.jpg",
    "http://[::ffff:127.0.0.1]/image.jpg",
    "http://[2001:db8::1]/image.jpg",
    "https://media.example:0/image.jpg",
    "https://media.example:99999/image.jpg",
    "https://media.example:5432/image.jpg",
    "http://media.example:9200/image.jpg",
    "https://media.example:80/image.jpg",
    "http://media.example:443/image.jpg",
    "https://media.example/image.jpg\nHost: localhost",
])
async def test_rejects_invalid_or_non_public_targets(monkeypatch, url):
    async def unexpected_lookup(*_args):
        raise AssertionError("invalid URL must not trigger DNS")

    monkeypatch.setattr(vision, "_lookup_addresses", unexpected_lookup)
    with pytest.raises(ValueError):
        await vision._public_fetch_target(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", [
    ["127.0.0.1"],
    ["93.184.216.34", "10.0.0.1"],
    ["2001:db8::1"],
    [],
])
async def test_rejects_dns_with_non_public_answer(monkeypatch, answers):
    lookups = mock_dns(monkeypatch, {"media.example": answers})
    with pytest.raises(ValueError):
        await vision._public_fetch_target("https://media.example/image.jpg")
    assert lookups == [("media.example", 443)]


@pytest.mark.asyncio
async def test_pins_public_ip_and_preserves_host_and_sni(monkeypatch):
    lookups = mock_dns(monkeypatch, {"media.example": ["93.184.216.34"]})
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "image/jpeg; charset=binary"},
            stream=MemoryStream(b"\xff\xd8\xffimage"),
        )

    mock_image_client(monkeypatch, handler)
    result = await vision.VisualOSINTTracker()._download_and_encode_image(
        "https://media.example/image.jpg?size=large",
    )

    assert base64.b64decode(result) == b"\xff\xd8\xffimage"
    assert lookups == [("media.example", 443)]
    assert len(requests) == 1
    assert str(requests[0].url) == "https://93.184.216.34/image.jpg?size=large"
    assert requests[0].headers["host"] == "media.example"
    assert requests[0].extensions["sni_hostname"] == "media.example"
    assert requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_redirect_to_private_ip_is_never_requested(monkeypatch):
    mock_dns(monkeypatch, {"media.example": ["93.184.216.34"]})
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    mock_image_client(monkeypatch, handler)
    assert await vision.VisualOSINTTracker()._download_and_encode_image(
        "https://media.example/image.jpg",
    ) is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_public_redirect_is_resolved_and_pinned_again(monkeypatch):
    lookups = mock_dns(monkeypatch, {
        "media.example": ["93.184.216.34"],
        "cdn.example": ["8.8.8.8"],
    })
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://cdn.example/photo.png"})
        return httpx.Response(
            200, headers={"content-type": "image/png"},
            stream=MemoryStream(b"\x89PNG\r\n\x1a\nimage"),
        )

    mock_image_client(monkeypatch, handler)
    result = await vision.VisualOSINTTracker()._download_and_encode_image(
        "https://media.example/image.jpg",
    )
    assert base64.b64decode(result) == b"\x89PNG\r\n\x1a\nimage"
    assert lookups == [("media.example", 443), ("cdn.example", 443)]
    assert [str(request.url) for request in requests] == [
        "https://93.184.216.34/image.jpg", "https://8.8.8.8/photo.png",
    ]
    assert requests[1].headers["host"] == "cdn.example"
    assert requests[1].extensions["sni_hostname"] == "cdn.example"


@pytest.mark.asyncio
async def test_redirect_limit_is_enforced(monkeypatch):
    mock_dns(monkeypatch, {"media.example": ["93.184.216.34"]})
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/image.jpg"})

    mock_image_client(monkeypatch, handler)
    assert await vision.VisualOSINTTracker()._download_and_encode_image(
        "https://media.example/image.jpg",
    ) is None
    assert len(requests) == vision.MAX_REDIRECTS + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,body", [
    ({"content-type": "text/html"}, b"<html></html>"),
    ({"content-type": "image/svg+xml"}, b"<svg></svg>"),
    ({"content-type": "image/jpeg"}, b"<html>fake image</html>"),
    ({"content-type": "image/jpeg", "content-encoding": "gzip"}, b"compressed"),
    ({"content-type": "image/jpeg", "content-length": str(vision.MAX_IMAGE_BYTES + 1)}, b"small"),
    ({"content-type": "image/jpeg", "content-length": "-1"}, b"small"),
])
async def test_rejects_unsupported_or_oversized_response(monkeypatch, headers, body):
    mock_dns(monkeypatch, {"media.example": ["93.184.216.34"]})
    mock_image_client(monkeypatch, lambda _request: httpx.Response(200, headers=headers, content=body))
    assert await vision.VisualOSINTTracker()._download_and_encode_image(
        "https://media.example/image.jpg",
    ) is None


@pytest.mark.asyncio
async def test_streamed_body_is_bounded_without_content_length(monkeypatch):
    mock_dns(monkeypatch, {"media.example": ["93.184.216.34"]})
    yielded = []

    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yielded.append(1)
            yield b"\x89PNG\r\n\x1a\n" + b"x" * (vision.MAX_IMAGE_BYTES // 2)
            yielded.append(2)
            yield b"x" * (vision.MAX_IMAGE_BYTES // 2 + 1)
            yielded.append(3)
            yield b"x"

    mock_image_client(monkeypatch, lambda _request: httpx.Response(
        200, headers={"content-type": "image/png"}, stream=OversizedStream(),
    ))
    assert await vision.VisualOSINTTracker()._download_and_encode_image(
        "https://media.example/image.jpg",
    ) is None
    assert yielded == [1, 2]


@pytest.mark.asyncio
async def test_invalid_image_keeps_safe_analysis_result(monkeypatch):
    def unexpected_client(*_args, **_kwargs):
        raise AssertionError("invalid image must not reach Ollama")

    monkeypatch.setattr(vision.httpx, "AsyncClient", unexpected_client)
    result = await vision.VisualOSINTTracker().analyze_image("http://127.0.0.1/private")
    assert result["risk_score"] == 0
    assert result["detected_objects"] == []


@pytest.mark.asyncio
async def test_dns_failure_returns_no_image_without_http_request(monkeypatch):
    async def failing_lookup(*_args):
        raise socket.gaierror("DNS failed")

    def unexpected_client(*_args, **_kwargs):
        raise AssertionError("DNS failure must not trigger HTTP request")

    monkeypatch.setattr(vision, "_lookup_addresses", failing_lookup)
    monkeypatch.setattr(vision.httpx, "AsyncClient", unexpected_client)
    assert await vision.VisualOSINTTracker()._download_and_encode_image(
        "https://media.example/image.jpg",
    ) is None
