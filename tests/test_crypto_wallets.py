from app.intel.crypto import CryptoOSINTTracker


def test_wallet_extraction_deduplicates_large_input_in_first_seen_order():
    addresses = [f"0x{number:040x}" for number in range(1_500)]
    text = " ".join(addresses + addresses[:50])

    wallets = CryptoOSINTTracker().extract_wallets(text)

    assert len(wallets) == len(addresses)
    assert [wallet["address"] for wallet in wallets] == addresses
    assert {wallet["currency"] for wallet in wallets} == {"ETH"}
