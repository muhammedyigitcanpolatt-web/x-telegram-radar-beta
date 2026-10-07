# app/intel/queries.py
"""
Cyber threat intelligence search templates.
The query literals intentionally preserve regional slang and operational terms
used by threat actors on X.
"""

from enum import Enum


class CampaignTemplate(Enum):
    """Search templates available to analysts through one endpoint."""
    NARCOTICS_MEXICO = "narcotics_mexico"
    SYNTHETICS_GLOBAL = "synthetics_global"
    WEAPONS_LOGISTICS = "weapons_logistics"
    OPERATIONAL_HIERARCHY = "operational_hierarchy"
    EMOJI_SUPPLY_CHAIN = "emoji_supply_chain"
    FINANCIAL_WASHING = "financial_washing"
    ALL_INCLUSIVE = "all_inclusive"


# ---------------------------------------------------------------------------
# 1. NARCOTICS LOGISTICS — Mexican and South American slang
# ---------------------------------------------------------------------------
NARCOTICS_MEXICO_QUERIES = [
    # Cocaine variants.
    '"lavada" OR "perico" OR "polvo blanco" OR "cuadro" OR "tuci"',
    '"cristal de roca" OR "nieve" OR "harina" OR "tabaco blanco"',
    # Heroin / fentanyl.
    '"china white" OR "azul" OR "M30" OR "M-30" OR "blues" OR "pildoras azules"',
    # Synthetic methamphetamine / fentanyl.
    '"hielo" OR "cristal" OR "speed" OR "glass" OR "anfetas"',
]

# ---------------------------------------------------------------------------
# 2. WEAPONS AND TACTICAL AMMUNITION CODES
# ---------------------------------------------------------------------------
WEAPONS_LOGISTICS_QUERIES = [
    # AK-47 and .50 caliber.
    '"cuerno de chivo" OR "cuernos de chivo" OR "tostón" OR "toston"',
    # RPK, AR-15, rocket launchers.
    '"pechera" OR "tubos" OR "cuerno" OR "lanza cohetes" OR "granada"',
    # Tactical equipment and armor.
    '"chaleco" OR "placas" OR "táctico" OR "blindado" OR "blindaje"',
    # Ammunition / magazines.
    '"cargador" OR "parque" OR "balas" OR "munición"',
]

# ---------------------------------------------------------------------------
# 3. OPERATIONAL AND HIERARCHICAL TERMINOLOGY
# ---------------------------------------------------------------------------
OPERATIONAL_HIERARCHY_QUERIES = [
    # Armed clashes / attacks / topón.
    '"topón" OR "enfrentamiento" OR "plaza" OR "territorio" OR "control"',
    # Kidnapping / levantón.
    '"levantón" OR "levantado" OR "secuestro" OR "plagiado" OR "levantar"',
    # Street lookouts (halcones).
    '"halcón" OR "halcones" OR "puntero" OR "punteo" OR "ojos" OR "vigía"',
    # Cartel hierarchy.
    '"jefe" OR "patrón" OR "comandante" OR "sicario" OR "escolta" OR "guardaespaldas"',
    # Threat messages / narcomanta.
    '"narcomanta" OR "narcomensaje" OR "cobro de piso" OR "derecho de piso"',
]

# ---------------------------------------------------------------------------
# 4. EMOJI COMBINATIONS
# ---------------------------------------------------------------------------
# A single emoji is ambiguous; combinations can carry a stronger signal.
EMOJI_SUPPLY_CHAIN_QUERIES = [
    # Logistics route: vehicle + parcel + power/plug.
    '(🚢 OR ✈️ OR 🚚) AND (📦 OR 🧱 OR 📬) AND (🔌 OR 🔋 OR ⚡)',
    # Financial flow: money + crypto + laundering.
    '(💸 OR 💰 OR 💵) AND (USDT OR TRX OR P2P OR TRC20) AND ("lavado" OR "washing" OR "limpieza")',
    # Communication code: key + snow + delivery.
    '(🔑 OR 🔐) AND (❄️ OR 🧊 OR 🍬) AND (📍 OR 📌 OR 🗺️)',
    # Weapons transport: protection + parcel + vehicle.
    '(🛡️ OR ⚔️) AND (📦 OR 🧳) AND (🚗 OR 🛻)',
]

# ---------------------------------------------------------------------------
# 5. FINANCIAL LAUNDERING (CHAIN HOPPING)
# ---------------------------------------------------------------------------
FINANCIAL_WASHING_QUERIES = [
    '"mixer" OR "tumbler" OR "swap" OR "bridge" AND (USDT OR USDC OR BUSD)',
    '"P2P" AND (Binance OR Huobi OR OKX) AND ("sin KYC" OR "no KYC" OR "anónimo")',
    '"intercambio" AND "cripto" AND ("efectivo" OR "cash" OR "cara a cara")',
]

# ---------------------------------------------------------------------------
# COMBINED QUERY ACROSS ALL TEMPLATES
# ---------------------------------------------------------------------------
ALL_INCLUSIVE_QUERY = " OR ".join([
    "(lavada OR perico OR polvo blanco)",
    "(cuerno de chivo OR tostón OR tubos)",
    "(topón OR levantón OR halcones OR punteo)",
    "(🚢 OR ✈️) AND (📦 OR 🧱) AND (🔌 OR 🔋)",
    "(💸 OR 💰) AND (USDT OR TRX) AND lavado",
    "(M30 OR blues OR hielo OR cristal)",
    "(narcomanta OR cobro de piso)",
])


TEMPLATES: dict[CampaignTemplate, dict] = {
    CampaignTemplate.NARCOTICS_MEXICO: {
        "label": "Mexican Narcotics Logistics",
        "queries": NARCOTICS_MEXICO_QUERIES,
        "description": "Mexican and South American slang for cocaine, fentanyl, and methamphetamine.",
    },
    CampaignTemplate.SYNTHETICS_GLOBAL: {
        "label": "Synthetic Drugs (Global)",
        "queries": [
            '"M30" OR "blues" OR "hielo" OR "cristal" OR "fentanilo" OR "sintético"',
            '"pastillas azules" OR "30 M" OR "fake oxy" OR "pressed"',
        ],
        "description": "Detect synthetic opioids and methamphetamine.",
    },
    CampaignTemplate.WEAPONS_LOGISTICS: {
        "label": "Weapons and Tactical Equipment",
        "queries": WEAPONS_LOGISTICS_QUERIES,
        "description": "AK-47, Barrett, tactical armor, and ammunition logistics.",
    },
    CampaignTemplate.OPERATIONAL_HIERARCHY: {
        "label": "Operational Hierarchy",
        "queries": OPERATIONAL_HIERARCHY_QUERIES,
        "description": "Armed clashes, kidnapping, street lookouts, and cartel hierarchy.",
    },
    CampaignTemplate.EMOJI_SUPPLY_CHAIN: {
        "label": "Emoji Supply Chain",
        "queries": EMOJI_SUPPLY_CHAIN_QUERIES,
        "description": "Detect logistics, finance, and weapons transport through emoji combinations.",
    },
    CampaignTemplate.FINANCIAL_WASHING: {
        "label": "Crypto Money Laundering",
        "queries": FINANCIAL_WASHING_QUERIES,
        "description": "Mixer, tumbler, P2P, and chain-hopping indicators.",
    },
    CampaignTemplate.ALL_INCLUSIVE: {
        "label": "Full Spectrum — All Threats",
        "queries": [ALL_INCLUSIVE_QUERY],
        "description": "A combined scan across every query template.",
    },
}


def get_queries_by_template(template: CampaignTemplate) -> list[str]:
    """Return the queries for the analyst's selected template."""
    entry = TEMPLATES.get(template)
    return entry["queries"] if entry else []


def list_available_templates() -> list[dict]:
    """Return template summaries for the dashboard and Swagger UI."""
    return [
        {
            "key": t.value,
            "label": info["label"],
            "description": info["description"],
            "query_count": len(info["queries"]),
        }
        for t, info in TEMPLATES.items()
    ]
