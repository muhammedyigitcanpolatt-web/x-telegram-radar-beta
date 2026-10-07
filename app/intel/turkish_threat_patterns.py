# app/intel/turkish_threat_patterns.py
"""
Threat patterns and slang specific to Turkish-language sources.
Adapted from the Shortmox CTI Turkish analysis engine in telegram_cti.
"""
import re

TURKISH_THREAT_PATTERNS = {
    "MADDE_TICARETI": [
        r"\bot\b", r"\bhap\b", r"\btorba\b", r"\bkenevir\b", r"\besrar\b",
        r"\bkokain\b", r"\bmet\b", r"\bgram\b", r"\bkiloluk\b", r"\bextacy\b",
        r"\bmdma\b", r"\bhint\b", r"\bafyon\b", r"\buyusturucu\b", r"\bseker\b",
        r"\btoz\b", r"\bpudra\b", r"\bkar\b", r"sh0p", r"torbaci", r"koko",
        r"amfetamin", r"paket teslimat", r"kubar", r"skunk", r"bonzai",
        r"lyrica", r"gerica", r"saksi", r"\btas\b", r"corbaci",
        r"elden teslim", r"konum atilir"
    ],
    "SAHTECILIK_FINANS": [
        r"\bcc\b", r"\bcvv\b", r"\bdump\b", r"\bbin\b", r"\bkart\b", r"\bklon\b",
        r"\bbanka\b", r"\bswift\b", r"\biban\b", r"\bhesap\b", r"\bkredi\b",
        r"\bpos\b", r"\bpasif\b", r"\baktif\b", r"\bvirtual\b", r"\bmastercard\b",
        r"\bvisa\b", r"sahte para", r"clon card", r"cc sat", r"ibandol",
        r"bakiye yukleme", r"pos cekim", r"stranger cc", r"striking",
        r"3d bypass", r"\bood\b", r"kart patlatma", r"mcc id",
        r"kiralik papara", r"kiralik iban", r"gecici iban", r"fiziki pos",
        r"bozum yapilir", r"komisyonlu cekim", r"kripto bozum"
    ],
    "ILLEGAL_HIZMET_VE_VERI_SIZINTISI": [
        r"\bhack\b", r"\bsiber\b", r"\bddos\b", r"\bbotnet\b", r"\bexploit\b",
        r"\bransomware\b", r"\brat\b", r"\bstealer\b", r"\bchecker\b", r"\bproxy\b",
        r"\bspoof\b", r"\bphish\b", r"\bcracker\b", r"\bshell\b", r"\blogger\b",
        r"\bloader\b", r"escort", r"fuhus", r"hackleme", r"ddos attack",
        r"log sat", r"tckn sorgu", r"panel sorgu", r"kimlik sorgu",
        r"vesika", r"mernis", r"ad soyad sorgu", r"plaka sorgu", r"uni sorgu",
        r"sulale sorgu", r"gsm sorgu", r"tapu sorgu", r"sgk sorgu", r"vip panel",
        r"database satilir", r"data satilir"
    ],
}

# IOC Indicators of Compromise
PHONE_PATTERN = re.compile(r"\b(?:\+90|0)?[ \-]?5\d{2}[ \-]?\d{3}[ \-]?\d{2}[ \-]?\d{2}\b")


def analyze_turkish_text(text: str) -> tuple[list[str], int]:
    """
    Classify Turkish text into threat categories.
    Return (matched_categories, confidence_score).
    """
    if not text:
        return [], 0

    found_categories = []
    text_lower = text.lower()

    for category, patterns in TURKISH_THREAT_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                found_categories.append(category)
                break

    categories = list(set(found_categories))
    confidence = 70
    if len(categories) >= 2:
        confidence = 85

    return categories, confidence
