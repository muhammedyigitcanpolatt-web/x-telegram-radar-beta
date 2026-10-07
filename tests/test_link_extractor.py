import pytest

from app.intel.link_extractor import TelegramLinkHunter


class FakeConnection:
    def __init__(self):
        self.inserted_urls = set()

    async def execute(self, sql, url, source_platform, source_id):
        assert "INSERT INTO discovered_telegram_links" in sql
        assert source_platform == "PUBLIC_OSINT"
        assert source_id == "fixture-source"
        if url in self.inserted_urls:
            return "INSERT 0 0"
        self.inserted_urls.add(url)
        return "INSERT 0 1"


class FakePool:
    def __init__(self):
        self.connection = FakeConnection()

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_invite_codes_are_stored_in_full_and_remain_distinct():
    pool = FakePool()
    hunter = TelegramLinkHunter()
    links = [
        "https://t.me/+AbC_123",
        "https://t.me/+XYZ-987",
        "https://t.me/joinchat/First_123",
        "https://t.me/joinchat/Second-987",
    ]

    inserted = await hunter.sniff_and_store_links(
        " ".join(links), "PUBLIC_OSINT", "fixture-source", pg_pool=pool
    )

    assert inserted == len(links)
    assert pool.connection.inserted_urls == set(links)

    duplicate = await hunter.sniff_and_store_links(
        links[0], "PUBLIC_OSINT", "fixture-source", pg_pool=pool
    )
    assert duplicate == 0


@pytest.mark.asyncio
async def test_public_channel_link_still_stores_exact_url():
    pool = FakePool()
    inserted = await TelegramLinkHunter().sniff_and_store_links(
        "See http://telegram.me/publicchannel now.",
        "PUBLIC_OSINT",
        "fixture-source",
        pg_pool=pool,
    )

    assert inserted == 1
    assert pool.connection.inserted_urls == {"https://telegram.me/publicchannel"}
