from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.scrapers.collection import collect_x_query, collection_source_status
from app.scrapers.osint_seeder import scan_osint_for_telegram_links
from app.scrapers.telegram_worker import collect_configured_channels
from app.scrapers.x_api import XApiError, normalize_x_query, search_recent_posts
from app.scrapers.x_client import XGraphQLClient, XGraphQLError
from app.scrapers.x_seeder_tg_links import SEARCH_QUERY, XTelegramLinkSeeder
from app.scrapers.x_source import XCollectionError, search_x_posts


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://api.fixture/ingest")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("fixture status", request=request, response=response)


class FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.expirations = {}
        self.closed = False

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.expirations.pop(key, None)
        return 1

    async def aclose(self):
        self.closed = True


class FakeHttpClient:
    def __init__(self, state, **_kwargs):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, *, headers=None, params=None):
        self.state["get"].append((url, headers, params))
        responses = self.state.get("get_responses")
        return responses.pop(0) if responses else self.state["get_response"]

    async def post(self, url, *, json, headers):
        self.state["post"].append((url, json, headers))
        responses = self.state.get("post_responses")
        return responses.pop(0) if responses else FakeResponse(200, {"status": "queued"})


def x_fixture_payload():
    return {
        "data": [
            {
                "id": "1900000000000000001",
                "author_id": "author-9",
                "text": "public fixture post",
                "created_at": "2026-10-03T10:00:00.000Z",
                "entities": {"urls": []},
            }
        ],
        "includes": {"users": [{
            "id": "author-9", "username": "fixture_user",
            "created_at": "2020-01-01T00:00:00.000Z",
        }]},
    }


def test_x_query_normalization_keeps_template_conjunction_and_quoted_word():
    assert normalize_x_query('(a OR b) AND "rock and roll"') == '(a OR b) "rock and roll"'


def test_default_x_collection_provider_is_scraping(monkeypatch):
    monkeypatch.delenv("X_COLLECTION_PROVIDER", raising=False)
    assert Settings(_env_file=None).X_COLLECTION_PROVIDER == "scraping"


def enabled_settings(**overrides):
    values = {
        "COLLECTION_ENABLED": True,
        "COLLECTION_MAX_RESULTS": 25,
        "COLLECTION_MAX_X_PAGES": 5,
        "COLLECTION_MAX_TELEGRAM_MESSAGES": 100,
        "X_COLLECTION_PROVIDER": "api",
        "X_BEARER_TOKEN": "fixture-x-token",
        "X_AUTH_TOKEN": "fixture-auth-token",
        "X_CT0": "fixture-ct0",
        "X_GRAPHQL_BEARER_TOKEN": "fixture-graphql-bearer",
        "TELEGRAM_API_ID": 12345,
        "TELEGRAM_API_HASH": "fixture-api-hash",
        "TELEGRAM_SESSION_STRING": "fixture-session",
        "TELEGRAM_TARGET_CHANNELS": "@public_fixture",
        "RADAR_INGEST_API_KEY": "fixture-ingest-key",
        "REDIS_URL": "redis://fixture/0",
        "BACKEND_INGEST_URL": "http://api.fixture/api/v1/ingest",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_x_recent_search_uses_bearer_api_and_maps_source_identity():
    state = {
        "get": [],
        "post": [],
        "get_response": FakeResponse(200, x_fixture_payload()),
    }
    records = await search_recent_posts(
        "fixture query",
        "fixture-x-token",
        max_results=25,
        since_id="1900000000000000000",
        http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
    )

    url, headers, params = state["get"][0]
    assert url == "https://api.x.com/2/tweets/search/recent"
    assert headers == {"Authorization": "Bearer fixture-x-token"}
    assert params["since_id"] == "1900000000000000000"
    assert params["max_results"] == 25
    assert "entities" in params["tweet.fields"]
    assert "created_at" in params["user.fields"]
    assert records.pages == 1
    assert records.next_token is None
    assert records.records[0]["source_platform"] == "X_TWITTER"
    assert records.records[0]["source_channel_id"] == "author-9"
    assert records.records[0]["source_message_id"] == "1900000000000000001"
    assert records.records[0]["tweet_id"] == "1900000000000000001"
    assert records.records[0]["username"] == "fixture_user"
    assert records.records[0]["account_created_at"] == "2020-01-01T00:00:00.000Z"


@pytest.mark.asyncio
async def test_x_graphql_client_uses_fixture_transport_and_passes_continuation_without_logging_secrets(caplog):
    state = {"calls": []}

    class Response:
        status_code = 200

        def json(self):
            return graphql_payload([])

    def request_get(url, **kwargs):
        import copy
        state["calls"].append((url, copy.deepcopy(kwargs)))
        return Response()

    client = XGraphQLClient(
        auth_token="fixture-auth-secret",
        ct0="fixture-csrf-secret",
        graphql_bearer_token="fixture-graphql-bearer",
        proxy_url="http://fixture-proxy:8080",
        request_get=request_get,
        graphql_base_url="https://x.fixture/i/api/graphql",
    )
    payload, status_code = await client.fetch_search_timeline(
        "fixture query", count=10, cursor="fixture-bottom-cursor"
    )

    url, kwargs = state["calls"][0]
    assert url.startswith("https://x.fixture/i/api/graphql/")
    assert kwargs["proxies"] == {
        "http": "http://fixture-proxy:8080", "https": "http://fixture-proxy:8080"
    }
    assert __import__("json").loads(kwargs["params"]["variables"])["cursor"] == "fixture-bottom-cursor"
    assert kwargs["headers"]["Cookie"].startswith("auth_token=fixture-auth-secret;")
    assert kwargs["headers"]["Authorization"] == "Bearer fixture-graphql-bearer"
    assert status_code == 200
    assert client.parse_graphql_page(payload) == ([], None)
    assert "fixture-auth-secret" not in caplog.text
    assert "fixture-csrf-secret" not in caplog.text
    assert "fixture-graphql-bearer" not in caplog.text


@pytest.mark.asyncio
async def test_x_collection_routes_posts_through_authenticated_ingest_and_deduplicates():
    config = enabled_settings()
    redis = FakeRedis()
    state = {
        "get": [],
        "post": [],
        "get_response": FakeResponse(200, x_fixture_payload()),
    }
    http_factory = lambda **kwargs: FakeHttpClient(state, **kwargs)
    redis_factory = lambda _url: redis

    first = await collect_x_query(
        "fixture query",
        config=config,
        http_client_factory=http_factory,
        redis_client_factory=redis_factory,
    )
    second = await collect_x_query(
        "fixture query",
        config=config,
        http_client_factory=http_factory,
        redis_client_factory=redis_factory,
    )

    assert first == {"status": "completed", "fetched": 1, "pages": 1, "continuation_pending": False, "queued": 1, "duplicates": 0}
    assert second == {"status": "completed", "fetched": 1, "pages": 1, "continuation_pending": False, "queued": 0, "duplicates": 1}
    assert len(state["post"]) == 1
    ingest_url, posted_record, headers = state["post"][0]
    assert ingest_url == config.BACKEND_INGEST_URL
    assert posted_record["source_message_id"] == "1900000000000000001"
    assert headers["X-Radar-Ingest-Key"] == "fixture-ingest-key"
    seen_key = next(key for key in redis.values if key.startswith("radar:collection:seen:"))
    assert redis.values[seen_key] == "done"
    assert redis.expirations[seen_key] == 30 * 24 * 60 * 60
    assert redis.closed is True


@pytest.mark.asyncio
async def test_x_collection_follows_two_pages_before_advancing_highwater_cursor():
    config = enabled_settings(COLLECTION_MAX_X_PAGES=2)
    redis = FakeRedis()
    first_page = {
        "data": [{"id": "200", "author_id": "a", "text": "page one"}],
        "includes": {"users": [{"id": "a", "username": "fixture"}]},
        "meta": {"next_token": "page-two"},
    }
    second_page = {
        "data": [{"id": "100", "author_id": "b", "text": "page two"}],
        "includes": {"users": [{"id": "b", "username": "fixture2"}]},
        "meta": {"result_count": 1},
    }
    state = {
        "get": [],
        "post": [],
        "get_responses": [FakeResponse(200, first_page), FakeResponse(200, second_page)],
    }

    result = await collect_x_query(
        "two page query",
        config=config,
        http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
        redis_client_factory=lambda _url: redis,
    )

    assert result["status"] == "completed"
    assert result["pages"] == 2
    assert result["fetched"] == 2
    assert state["get"][1][2]["next_token"] == "page-two"
    cursor = next(value for key, value in redis.values.items() if ":since_id" in key)
    assert cursor == "200"
    assert not any(":page:" in key for key in redis.values)
    assert not any(":highwater:" in key for key in redis.values)


@pytest.mark.asyncio
async def test_x_second_page_error_keeps_previous_cursor_and_page_state():
    config = enabled_settings(COLLECTION_MAX_X_PAGES=2)
    query = "second page error query"
    import hashlib
    query_key = hashlib.sha256(query.encode("utf-8")).hexdigest()[:24]
    cursor_key = f"radar:collection:x:api:{query_key}:since_id"
    redis = FakeRedis({cursor_key: "90"})
    first_page = {"data": [{"id": "120", "author_id": "a", "text": "page one"}], "meta": {"next_token": "next"}}
    state = {
        "get": [],
        "post": [],
        "get_responses": [FakeResponse(200, first_page), FakeResponse(429)],
    }

    with pytest.raises(XApiError, match="rate limit"):
        await collect_x_query(
            query,
            config=config,
            http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
            redis_client_factory=lambda _url: redis,
        )

    assert redis.values[cursor_key] == "90"
    assert not any(":page" in key or ":highwater" in key for key in redis.values)
    assert state["post"] == []


@pytest.mark.asyncio
async def test_ingest_queue_full_releases_short_claim_and_preserves_x_cursor_for_retry():
    config = enabled_settings(COLLECTION_MAX_X_PAGES=1)
    redis = FakeRedis()
    state = {
        "get": [],
        "post": [],
        "get_response": FakeResponse(200, x_fixture_payload()),
        "post_responses": [FakeResponse(503), FakeResponse(200)],
    }
    factory = lambda **kwargs: FakeHttpClient(state, **kwargs)

    with pytest.raises(httpx.HTTPStatusError):
        await collect_x_query(
            "queue full query", config=config,
            http_client_factory=factory,
            redis_client_factory=lambda _url: redis,
        )
    assert not any(":since_id" in key for key in redis.values)
    assert not any(key.startswith("radar:collection:seen:") for key in redis.values)

    result = await collect_x_query(
        "queue full query", config=config,
        http_client_factory=factory,
        redis_client_factory=lambda _url: redis,
    )
    assert result["queued"] == 1
    assert any(":since_id" in key for key in redis.values)


@pytest.mark.asyncio
async def test_x_api_errors_do_not_include_credentials():
    state = {"get": [], "post": [], "get_response": FakeResponse(403)}
    with pytest.raises(XApiError, match="not available") as error:
        await search_recent_posts(
            "fixture query",
            "secret-token-fixture",
            http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
        )
    assert "secret-token-fixture" not in str(error.value)


@pytest.mark.asyncio
async def test_x_link_seeder_uses_mocked_official_search_and_keeps_source_post_id():
    payload = x_fixture_payload()
    payload["data"][0]["text"] = "Join https://t.co/invite"
    payload["data"][0]["entities"]["urls"] = [{
        "url": "https://t.co/invite",
        "expanded_url": "https://t.me/+AbC_123",
    }]
    state = {"get": [], "post": [], "get_response": FakeResponse(200, payload)}
    seeder = XTelegramLinkSeeder(
        config=enabled_settings(),
        http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
    )

    records = await seeder.scan_for_telegram_link_records()

    assert records == [{"url": "https://t.me/+AbC_123", "source_id": "1900000000000000001"}]
    assert state["get"][0][0] == "https://api.x.com/2/tweets/search/recent"


@pytest.mark.asyncio
async def test_x_link_seeder_reuses_scraping_provider_and_expanded_graphql_urls():
    config = enabled_settings(X_COLLECTION_PROVIDER="scraping")
    payload = graphql_payload([
        graphql_tweet_entry(
            "42", "fixture_user", "invite fixture", urls=[{
                "url": "https://t.co/join", "expanded_url": "https://t.me/joinchat/fixtureCode",
            }],
        ),
    ])
    clients = []

    def make_client(**kwargs):
        client = FakeGraphQLClient([payload], **kwargs)
        clients.append(client)
        return client

    state = {"get": [], "post": []}
    seeder = XTelegramLinkSeeder(
        config=config,
        graphql_client_factory=make_client,
        http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
    )

    records = await seeder.scan_for_telegram_link_records()

    assert records == [{"url": "https://t.me/joinchat/fixtureCode", "source_id": "42"}]
    assert clients[0].calls[0]["query"] == SEARCH_QUERY
    assert state["get"] == []


@pytest.mark.asyncio
async def test_osint_link_seeder_uses_only_mocked_public_source_response():
    state = {
        "get": [],
        "post": [],
        "get_response": FakeResponse(
            200,
            text="Invite https://t.me/+AbC and legacy t.me/joinchat/Def_9",
        ),
    }

    links = await scan_osint_for_telegram_links(
        http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs)
    )

    assert set(links) == {"https://t.me/+AbC", "https://t.me/joinchat/Def_9"}
    assert state["get"]  # The fixture client served the source; no external client ran.


class AsyncRows:
    def __init__(self, rows):
        self.rows = rows

    def __aiter__(self):
        async def iterator():
            for row in self.rows:
                yield row
        return iterator()


class FakeTelegramClient:
    def __init__(self, messages):
        self.messages = messages
        self.disconnected = False
        self.iteration_args = []

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return True

    async def get_entity(self, channel):
        assert channel == "@public_fixture"
        return SimpleNamespace(chat_id="-100123", username="public_fixture")

    def iter_messages(self, entity, **kwargs):
        assert entity.chat_id == "-100123"
        self.iteration_args.append(kwargs)
        return AsyncRows(self.messages)

    async def disconnect(self):
        self.disconnected = True


@pytest.mark.asyncio
async def test_telegram_sweep_reads_configured_channel_without_joining_and_uses_canonical_ids():
    config = enabled_settings()
    redis = FakeRedis()
    messages = [
        SimpleNamespace(id=2, message="second fixture", date=datetime(2026, 10, 3, tzinfo=timezone.utc)),
        SimpleNamespace(id=1, message="first fixture", date=datetime(2026, 10, 2, tzinfo=timezone.utc)),
    ]
    client = FakeTelegramClient(messages)
    state = {"get": [], "post": [], "get_response": FakeResponse()}

    result = await collect_configured_channels(
        config=config,
        telegram_client_factory=lambda _config: client,
        redis_client_factory=lambda _url: redis,
        http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
    )

    assert result == {"status": "completed", "fetched": 2, "queued": 2, "duplicates": 0}
    assert client.disconnected is True
    assert len(state["post"]) == 2
    first = state["post"][0][1]
    second = state["post"][1][1]
    assert first["tweet_id"] == "telegram:-100123:1"
    assert first["source_channel_id"] == "-100123"
    assert second["tweet_id"] == "telegram:-100123:2"
    assert redis.values[next(key for key in redis.values if ":cursor:" in key)] == "2"


def test_collection_status_requires_explicit_opt_in_and_reports_only_missing_setting_names():
    config = enabled_settings(COLLECTION_ENABLED=False, X_BEARER_TOKEN="secret")
    disabled = collection_source_status("x", config)
    assert disabled["status"] == "disabled"
    assert disabled["provider"] == "api"

    configured_but_missing = enabled_settings(X_BEARER_TOKEN="")
    status = collection_source_status("x", configured_but_missing)
    assert status["status"] == "configuration_required"
    assert status["missing"] == ["X_BEARER_TOKEN"]
    assert "secret" not in str(status)

    scraping_missing = enabled_settings(
        X_COLLECTION_PROVIDER="scraping", X_AUTH_TOKEN="", X_CT0="",
        X_GRAPHQL_BEARER_TOKEN="",
    )
    scraping_status = collection_source_status("x", scraping_missing)
    assert scraping_status["missing"] == [
        "X_GRAPHQL_BEARER_TOKEN", "X_AUTH_TOKEN", "X_CT0"
    ]
    assert scraping_status["provider"] == "scraping"
    assert scraping_status["credential_source"] == "env"


def graphql_tweet_entry(tweet_id, username, text, *, wrapped=False, urls=None):
    legacy = {
        "id_str": str(tweet_id),
        "user_id_str": f"user-{username}",
        "full_text": text,
        "created_at": "Sat Oct 03 10:00:00 +0000 2026",
        "entities": {"urls": urls or []},
    }
    user = {"legacy": {
        "screen_name": username,
        "created_at": "Wed Jan 01 00:00:00 +0000 2020",
    }}
    result = {"legacy": legacy, "core": {"user_results": {"result": user}}}
    if wrapped:
        result = {"tweet": result}
    return {
        "entryId": f"tweet-{tweet_id}",
        "content": {"itemContent": {"tweet_results": {"result": result}}},
    }


def graphql_cursor_entry(entry_id, value, *, direction=None):
    cursor = {"value": value}
    if direction:
        cursor["cursorType"] = direction
    return {
        "entryId": entry_id,
        "content": {"operation": {"cursor": cursor}},
    }


def graphql_payload(entries):
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {"timeline": {"instructions": [{"entries": entries}]}}
            }
        }
    }


class FakeGraphQLClient(XGraphQLClient):
    def __init__(self, responses, status_codes=None, **kwargs):
        super().__init__(**kwargs)
        self.responses = list(responses)
        self.status_codes = list(status_codes or [200] * len(responses))
        self.calls = []

    async def fetch_search_timeline(self, query, count=40, cursor=None):
        self.calls.append({"query": query, "count": count, "cursor": cursor})
        return self.responses.pop(0), self.status_codes.pop(0)


@pytest.mark.asyncio
async def test_default_provider_is_scraping_and_parser_handles_wrapped_and_malformed_entries():
    config = enabled_settings(
        X_COLLECTION_PROVIDER="scraping",
        X_AUTH_TOKEN="fixture-auth-token",
        X_CT0="fixture-ct0",
    )
    first_page = graphql_payload([
        {"entryId": "tweet-malformed", "content": {"itemContent": {"tweet_results": "broken"}}},
        graphql_tweet_entry(
            "200", "normal_user", "normal fixture", urls=[{
                "url": "https://t.co/invite", "expanded_url": "https://t.me/+fixtureCode",
            }],
        ),
        graphql_tweet_entry("199", "wrapped_user", "wrapped fixture", wrapped=True),
        graphql_cursor_entry("cursor-top-1", "top-cursor"),
        graphql_cursor_entry("cursor-bottom-1", "bottom-cursor"),
    ])
    # A top-only cursor must not be mistaken for a continuation cursor.
    second_page = graphql_payload([graphql_cursor_entry("cursor-top-2", "top-only")])
    clients = []

    def make_client(**kwargs):
        client = FakeGraphQLClient([first_page, second_page], **kwargs)
        clients.append(client)
        return client

    state = {"get": [], "post": []}
    result = await search_x_posts(
        "fixture query",
        config=config,
        max_results=10,
        max_pages=2,
        graphql_client_factory=make_client,
        http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
    )

    assert result.pages == 2
    assert result.next_token is None
    assert [record["source_message_id"] for record in result.records] == ["200", "199"]
    assert result.records[0]["source_platform"] == "X_TWITTER"
    assert result.records[0]["account_created_at"] == "Wed Jan 01 00:00:00 +0000 2020"
    assert result.records[0]["expanded_urls"] == ["https://t.me/+fixtureCode"]
    assert result.records[1]["username"] == "wrapped_user"
    assert clients[0].calls[1]["cursor"] == "bottom-cursor"
    assert state["get"] == []  # scraping mode never falls through to the API client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (401, "credentials were rejected"),
        (403, "denied the scraping request"),
        (429, "rate limited the scraping request"),
    ],
)
async def test_scraping_http_errors_are_distinct_and_never_fallback_to_api(status_code, message):
    config = enabled_settings(X_COLLECTION_PROVIDER="scraping")
    client = FakeGraphQLClient([{}], status_codes=[status_code], auth_token="secret", ct0="secret")
    state = {"get": [], "post": []}

    with pytest.raises(XCollectionError, match=message) as error:
        await search_x_posts(
            "fixture query",
            config=config,
            max_results=10,
            graphql_client_factory=lambda **_kwargs: client,
            http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
        )

    assert error.value.status_code == status_code
    assert "secret" not in str(error.value)
    assert len(client.calls) == 1
    assert state["get"] == []


@pytest.mark.asyncio
async def test_scraping_rejects_empty_and_malformed_graphql_payloads_distinctly():
    config = enabled_settings(X_COLLECTION_PROVIDER="scraping")
    for payload, message in (
        ({}, "empty response"),
        ([], "malformed timeline"),
        ({"data": {}}, "malformed timeline"),
    ):
        client = FakeGraphQLClient([payload], auth_token="fixture-auth-token", ct0="fixture-ct0")
        with pytest.raises(XGraphQLError, match=message):
            await search_x_posts(
                "fixture query",
                config=config,
                max_results=10,
                graphql_client_factory=lambda **_kwargs: client,
            )


@pytest.mark.asyncio
async def test_scraping_watermark_and_api_continuation_keys_are_provider_scoped():
    config = enabled_settings(X_COLLECTION_PROVIDER="scraping")
    query = "provider-specific state"
    import hashlib
    query_key = hashlib.sha256(query.encode("utf-8")).hexdigest()[:24]
    api_page = f"radar:collection:x:api:{query_key}:page"
    api_since = f"radar:collection:x:api:{query_key}:since_id"
    watermark = f"radar:collection:x:scraping:{query_key}:watermark"
    redis = FakeRedis({api_page: "api-token", api_since: "90", watermark: "100"})
    payload = graphql_payload([
        graphql_tweet_entry("101", "fixture", "newer post"),
        graphql_tweet_entry("100", "fixture", "watermark post"),
        graphql_tweet_entry("99", "fixture", "older post"),
        graphql_cursor_entry("cursor-bottom-1", "old-page"),
    ])
    client = FakeGraphQLClient([payload], auth_token="fixture-auth-token", ct0="fixture-ct0")
    state = {"get": [], "post": []}

    result = await collect_x_query(
        query,
        config=config,
        graphql_client_factory=lambda **_kwargs: client,
        http_client_factory=lambda **kwargs: FakeHttpClient(state, **kwargs),
        redis_client_factory=lambda _url: redis,
    )

    assert result["status"] == "completed"
    assert result["fetched"] == 1
    assert client.calls[0]["cursor"] is None
    assert [call[1]["source_message_id"] for call in state["post"]] == ["101"]
    assert redis.values[api_page] == "api-token"
    assert redis.values[api_since] == "90"
    assert redis.values[watermark] == "101"


@pytest.mark.asyncio
async def test_env_credentials_can_select_existing_fleet_proxy_without_live_database(monkeypatch):
    import app.scrapers.puppet_master as puppet_module

    class FixtureFleet:
        def __init__(self):
            self.proxy_calls = 0

        async def get_random_proxy(self):
            self.proxy_calls += 1
            return "http://proxy.fixture:8080"

    fleet = FixtureFleet()
    monkeypatch.setattr(puppet_module, "FleetCommander", lambda: fleet)
    config = enabled_settings(
        X_COLLECTION_PROVIDER="scraping",
        X_SCRAPING_CREDENTIAL_SOURCE="env",
        X_PROXY_SOURCE="fleet",
        X_AUTH_TOKEN="fixture-auth-token",
        X_CT0="fixture-ct0",
    )
    captured = {}
    payload = graphql_payload([])

    def make_client(**kwargs):
        captured.update(kwargs)
        return FakeGraphQLClient([payload], **kwargs)

    result = await search_x_posts(
        "fixture query", config=config, max_results=10, graphql_client_factory=make_client
    )

    assert result.records == []
    assert fleet.proxy_calls == 1
    assert captured["auth_token"] == "fixture-auth-token"
    assert captured["proxy_url"] == "http://proxy.fixture:8080"


@pytest.mark.asyncio
async def test_fleet_account_status_is_reported_once_without_account_rotation(monkeypatch):
    import app.scrapers.puppet_master as puppet_module

    class FixtureFleet:
        def __init__(self):
            self.checkout_calls = 0
            self.casualties = []

        async def checkout_healthy_account(self):
            self.checkout_calls += 1
            return {"id": 41, "auth_token": "fixture-auth-token", "ct0": "fixture-ct0"}

        async def report_casualty(self, account_id, status_code):
            self.casualties.append((account_id, status_code))

        async def get_random_proxy(self):
            return "http://proxy.fixture:8080"

    fleet = FixtureFleet()
    monkeypatch.setattr(puppet_module, "FleetCommander", lambda: fleet)
    config = enabled_settings(
        X_COLLECTION_PROVIDER="scraping",
        X_SCRAPING_CREDENTIAL_SOURCE="fleet",
        X_PROXY_SOURCE="fleet",
    )
    client = FakeGraphQLClient([{}], status_codes=[429], auth_token="fixture-auth-token", ct0="fixture-ct0")

    with pytest.raises(XCollectionError, match="rate limited") as error:
        await search_x_posts(
            "fixture query", config=config, max_results=10,
            graphql_client_factory=lambda **_kwargs: client,
        )

    assert error.value.status_code == 429
    assert fleet.checkout_calls == 1
    assert fleet.casualties == [(41, 429)]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_ops_routes_gate_and_enqueue_collection_tasks(monkeypatch):
    import app.routers.tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "collection_source_status",
        lambda _source: {"status": "ready", "reason": None, "missing": []},
    )
    enqueued = []

    class FakeTask:
        def __init__(self, name):
            self.name = name

        def apply_async(self, args, queue):
            enqueued.append((self.name, args, queue))
            return SimpleNamespace(id=f"task-{len(enqueued)}")

    names = (
        "tasks.light_ingest_sweep",
        "tasks.campaign_ingest_sweep",
        "tasks.telegram_channel_sweep",
        "tasks.x_telegram_link_seeder",
        "tasks.osint_telegram_link_seeder",
    )
    monkeypatch.setattr(
        tasks_module,
        "celery",
        SimpleNamespace(tasks={name: FakeTask(name) for name in names}),
    )

    assert (await tasks_module.trigger_intelligence_sweep("fixture query"))["task_id"] == "task-1"
    campaign = await tasks_module.trigger_campaign_sweep("narcotics_mexico")
    assert campaign["campaign"] == "narcotics_mexico"
    assert (await tasks_module.trigger_telegram_sweep())["task_id"] == "task-3"
    assert (await tasks_module.trigger_link_seeder("x"))["source"] == "x"
    assert (await tasks_module.trigger_link_seeder("osint"))["source"] == "osint"
    assert all(item[2] == "light_ops" for item in enqueued)
    assert enqueued[0][1] == ["fixture query"]


@pytest.mark.asyncio
async def test_ops_sweep_returns_clear_503_when_collection_is_off(monkeypatch):
    import app.routers.tasks as tasks_module

    monkeypatch.setattr(
        tasks_module,
        "collection_source_status",
        lambda _source: {
            "status": "disabled",
            "reason": "Source collection is off; set COLLECTION_ENABLED=true to enable it.",
            "missing": [],
        },
    )
    with pytest.raises(HTTPException) as error:
        await tasks_module.trigger_intelligence_sweep("fixture query")
    assert error.value.status_code == 503
    assert "COLLECTION_ENABLED=true" in error.value.detail
