import asyncio
import logging
import json
import random
import re
from typing import Any, Callable

from curl_cffi import requests as curl_requests
from app.config import settings
from app.auth import ingest_service_headers

logger = logging.getLogger("X_Cartel_Scraper")


class XGraphQLError(RuntimeError):
    """A safe-to-report failure from the existing X GraphQL collector."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def sanitize_log_data(data_str: str) -> str:
    """Mask session cookies before including request data in logs."""
    sanitized = re.sub(r"auth_token=[a-zA-Z0-9]+", "auth_token=********", data_str)
    sanitized = re.sub(r"ct0=[a-zA-Z0-9]+", "ct0=********", sanitized)
    return sanitized


class DeviceSpoofer:
    """Build browser-compatible headers for the configured X account."""

    DEVICE_PROFILES = [
        {"device_model": "Samsung Galaxy S23 Ultra", "system_version": "Android 14", "app_version": "10.14.5"},
        {"device_model": "iPhone 15 Pro Max", "system_version": "iOS 17.4", "app_version": "10.14.1"},
        {"device_model": "Xiaomi 14 Pro", "system_version": "Android 14", "app_version": "10.13.0"},
        {"device_model": "Google Pixel 8", "system_version": "Android 14", "app_version": "10.15.2"},
    ]

    @staticmethod
    def generate_stealth_headers(auth_token: str, ct0: str, bearer_token: str) -> dict:
        memory_sizes = ["8", "16", "32"]
        cpu_cores = ["4", "8", "12", "16"]
        platforms = ['"Windows"', '"macOS"']
        device = random.choice(DeviceSpoofer.DEVICE_PROFILES)

        return {
            "Authorization": f"Bearer {bearer_token}",
            "X-Twitter-Auth-Type": "OAuth2Session",
            "X-Twitter-Active-User": "yes",
            "X-Csrf-Token": ct0,
            "Cookie": f"auth_token={auth_token}; ct0={ct0};",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": random.choice(platforms),
            "Device-Memory": random.choice(memory_sizes),
            "Hardware-Concurrency": random.choice(cpu_cores),
            "Accept-Language": "en-US,en;q=0.9",
            "X-Client-UUID": device["device_model"].replace(" ", "_"),
        }


CARTEL_TARGET_QUERIES = [
    '"cjng" OR "cds" OR "kartel" OR "sicario"',
    '🔑❄️ OR 🍬✈️ OR 🔌📦',
    '"plaza" "comandante" OR "limpia"',
    '"monero" OR "usdt" "entrega"'
]


async def get_active_queries() -> list[str]:
    """Return approved lexicon terms, or the static query list if empty."""
    from app.database import db
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT term FROM target_lexicon WHERE is_active = TRUE AND review_status = 'APPROVED' ORDER BY last_used_at DESC"
        )
        terms = [r["term"] for r in rows]
        # Use the static list when the database contains no approved terms.
        return terms if terms else CARTEL_TARGET_QUERIES


class XGraphQLClient:
    def __init__(
        self,
        auth_token: str = None,
        ct0: str = None,
        graphql_bearer_token: str = None,
        proxy_url: str = None,
        accounts_pool: list[dict] = None,
        request_get: Callable[..., Any] | None = None,
        graphql_base_url: str | None = None,
    ):
        """Support both account pool and Celery single-task collection modes."""
        self.accounts_pool = accounts_pool or []
        self.current_account_index = 0
        self.base_url = (graphql_base_url or "https://x.com/i/api/graphql").rstrip("/")
        self.request_get = request_get or curl_requests.get
        self.backend_ingest_url = getattr(settings, "BACKEND_INGEST_URL", "http://127.0.0.1:8005/api/v1/ingest")

        self.is_celery_mode = bool(auth_token and ct0)
        self.active_auth_token = auth_token
        self.active_ct0 = ct0
        self.graphql_bearer_token = graphql_bearer_token
        self.active_proxy = proxy_url

    def _get_current_headers(self) -> dict:
        """Build request headers for the assigned or pooled account."""
        if self.is_celery_mode:
            token, csrf = self.active_auth_token, self.active_ct0
        else:
            if not self.accounts_pool:
                logger.error("[CRITICAL] Account pool is empty; request headers unavailable.")
                return {}
            account = self.accounts_pool[self.current_account_index]
            token, csrf = account["auth_token"], account["ct0"]
        if not self.graphql_bearer_token:
            raise XGraphQLError("X GraphQL bearer token is not configured.")
        return DeviceSpoofer.generate_stealth_headers(
            token, csrf, self.graphql_bearer_token
        )

    def _get_current_proxy(self) -> str | None:
        """Return the proxy assigned to the current Celery task."""
        if self.is_celery_mode:
            return self.active_proxy
        return None

    async def fetch_search_timeline(
        self, query: str, count: int = 40, cursor: str | None = None
    ) -> tuple[dict, int]:
        """Fetch the Latest timeline and return (response data, HTTP status)."""
        features = {
            "rweb_tipjar_consumption_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "unified_cards_ad_metadata_container_dynamic_card_content_query_enabled": True,
            "viewer_v2_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "tweet_awards_web_tipping_enabled": False,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_utf8_enabled": True,
            "rweb_video_timestamps_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_enhance_cards_enabled": False
        }

        variables = {
            "rawQuery": query,
            "count": count,
            "querySource": "typed_query",
            "product": "Latest"
        }
        if cursor:
            variables["cursor"] = cursor

        endpoint_id = "nK_v6wZub6wPRN3_8guisg"
        url = f"{self.base_url}/{endpoint_id}/SearchTimeline"

        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(features)
        }

        proxy = self._get_current_proxy()
        proxies = {"http": proxy, "https": proxy} if proxy else None

        headers = self._get_current_headers()
        if not headers:
            return {}, 500
        try:
            response = await asyncio.to_thread(
                self.request_get,
                url,
                headers=headers,
                params=params,
                impersonate="chrome120",
                proxies=proxies,
                allow_redirects=False,
                verify=True,
                timeout=15
            )

            if response.status_code == 200:
                try:
                    payload = response.json()
                except (ValueError, TypeError):
                    logger.warning("X GraphQL returned non-JSON content (HTTP 200).")
                    return {}, 200
                return payload, 200
            logger.warning("X GraphQL returned HTTP %d.", response.status_code)
            return {}, response.status_code
        except Exception as exc:
            # Do not log exception text: HTTP clients may include cookie/proxy details.
            logger.error("X GraphQL request failed (%s).", type(exc).__name__)
            return {}, 500
        finally:
            from app.utils.opsec import secure_headers_cleanup
            secure_headers_cleanup(headers)
            if proxies:
                proxies.clear()

    async def fetch_search_timeline_with_status(
        self, query: str, count: int = 40, cursor: str | None = None
    ) -> tuple[dict, int]:
        """Alias for fetch_search_timeline for explicit SOAR/Celery compatibility."""
        return await self.fetch_search_timeline(query, count, cursor)

    @staticmethod
    def _timeline(raw_data: dict) -> dict:
        if raw_data is None or raw_data == {}:
            raise XGraphQLError("X GraphQL returned an empty response.")
        if not isinstance(raw_data, dict):
            raise XGraphQLError("X GraphQL returned a malformed timeline response.")
        if raw_data.get("errors"):
            raise XGraphQLError("X GraphQL returned an error payload.")
        try:
            timeline = raw_data["data"]["search_by_raw_query"]["search_timeline"]["timeline"]
        except (KeyError, TypeError):
            raise XGraphQLError("X GraphQL returned a malformed timeline response.") from None
        if not isinstance(timeline, dict) or not isinstance(timeline.get("instructions"), list):
            raise XGraphQLError("X GraphQL returned a malformed timeline response.")
        return timeline

    def parse_graphql_page(self, raw_data: dict) -> tuple[list[dict], str | None]:
        """Normalize one fixture/live GraphQL timeline page and extract its bottom cursor."""
        timeline = self._timeline(raw_data)
        tweets: list[dict] = []
        next_cursor = None

        for instruction in timeline["instructions"]:
            if not isinstance(instruction, dict):
                continue
            entries = instruction.get("entries") or []
            if instruction.get("entry"):
                entries = [*entries, instruction["entry"]]
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                content = entry.get("content") or {}
                entry_id = str(entry.get("entryId") or "")
                operation = content.get("operation") if isinstance(content, dict) else None
                operation_cursor = operation.get("cursor") if isinstance(operation, dict) else None
                direct_cursor = content.get("cursor") if isinstance(content, dict) else None
                cursor_value = next((
                    candidate.get("value")
                    for candidate in (operation_cursor, direct_cursor, content)
                    if isinstance(candidate, dict) and candidate.get("value")
                ), None)
                cursor_type = ""
                if isinstance(content, dict):
                    operation_cursor_type = (
                        operation_cursor.get("cursorType")
                        if isinstance(operation_cursor, dict) else None
                    )
                    cursor_type = str(
                        content.get("cursorType")
                        or content.get("cursor_type")
                        or operation_cursor_type
                        or ""
                    ).lower()
                is_bottom_cursor = "cursor-bottom" in entry_id or cursor_type == "bottom"
                if cursor_value and is_bottom_cursor:
                    next_cursor = str(cursor_value)
                if not entry_id.startswith("tweet-") or not isinstance(content, dict):
                    continue
                item = (
                    content.get("itemContent") or content.get("item") or {}
                )
                tweet_results = item.get("tweet_results") if isinstance(item, dict) else None
                result = tweet_results.get("result") if isinstance(tweet_results, dict) else None
                if not isinstance(result, dict):
                    continue
                if isinstance(result, dict) and result.get("tweet"):
                    result = result["tweet"]
                legacy = result.get("legacy") if isinstance(result, dict) else None
                if not legacy and isinstance(result, dict):
                    wrapped_tweet = result.get("tweet")
                    legacy = wrapped_tweet.get("legacy") if isinstance(wrapped_tweet, dict) else None
                core = result.get("core") if isinstance(result, dict) else {}
                user_results = core.get("user_results") if isinstance(core, dict) else None
                user_result = user_results.get("result") if isinstance(user_results, dict) else None
                if not isinstance(user_result, dict):
                    user_result = {}
                user_legacy = user_result.get("legacy") or {}
                user_core = user_result.get("core") or {}
                if not isinstance(user_legacy, dict):
                    user_legacy = {}
                if not isinstance(user_core, dict):
                    user_core = {}
                username = (
                    user_legacy.get("screen_name")
                    or user_core.get("screen_name")
                    or user_core.get("name")
                    or "unknown"
                )
                if not isinstance(legacy, dict):
                    continue
                tweet_id = legacy.get("id_str") or (result or {}).get("rest_id")
                if not tweet_id:
                    continue
                user_id = str(legacy.get("user_id_str") or user_result.get("rest_id") or "")
                expanded_urls = []
                entities = legacy.get("entities") or {}
                for url in entities.get("urls", []) if isinstance(entities, dict) else []:
                    if isinstance(url, dict):
                        expanded = url.get("expanded_url") or url.get("url")
                        if isinstance(expanded, str) and expanded:
                            expanded_urls.append(expanded)
                from app.source_identity import normalize_source_record
                record = normalize_source_record({
                    "source_platform": "X_TWITTER",
                    "source_channel_id": user_id,
                    "source_message_id": str(tweet_id),
                    "tweet_id": str(tweet_id),
                    "user_id": user_id,
                    "username": username,
                    "text": legacy.get("full_text") or legacy.get("text") or "",
                    "created_at": legacy.get("created_at"),
                    "account_created_at": user_legacy.get("created_at"),
                })
                record["expanded_urls"] = expanded_urls
                tweets.append(record)
        return tweets, next_cursor

    def parse_graphql_response(self, raw_data: dict) -> list[dict]:
        """Normalize a GraphQL response into source records."""
        return self.parse_graphql_page(raw_data)[0]

    async def stream_to_radar_backend(self, tweets: list[dict]):
        """Submit normalized source records to the authenticated ingest API."""
        try:
            backend_headers = {
                "Content-Type": "application/json",
                **ingest_service_headers(settings),
            }
        except RuntimeError as exc:
            logger.error("Ingest service key is not configured: %s", exc)
            return

        for tweet in tweets:
            try:
                response = await asyncio.to_thread(
                    curl_requests.post,
                    self.backend_ingest_url,
                    headers=backend_headers,
                    data=json.dumps(tweet),
                    timeout=5
                )
                if response.status_code == 200:
                    logger.info(
                        "Ingested an X source record for user %s.", tweet["username"]
                    )
            except Exception as exc:
                # HTTP client exceptions may embed credentials or proxy URLs.
                logger.error("Backend ingest failed (%s).", type(exc).__name__)
