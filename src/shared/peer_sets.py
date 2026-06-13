"""Hand-curated peer sets for sector/industry lookup.

Used by both the Market Context Agent and the Quant sector comparison node.
Lookup is first by industry, then by sector, with key normalization so that
yfinance's "Software - Infrastructure" (hyphen + spaces) matches our stored
"Software—Infrastructure" (em-dash) and vice versa.
"""

import re

_PEER_SETS: dict[str, list[str]] = {
    # ── Technology ──────────────────────────────────────────────────────────
    "Software—Infrastructure": ["MSFT", "ORCL", "NOW", "CRM", "ADBE"],
    "Software - Infrastructure": ["MSFT", "ORCL", "NOW", "CRM", "ADBE"],
    "Software—Application": ["CRM", "NOW", "ADBE", "INTU", "WDAY"],
    "Software - Application": ["CRM", "NOW", "ADBE", "INTU", "WDAY"],
    "Semiconductors": ["NVDA", "AMD", "INTC", "AVGO", "TSM"],
    "Semiconductor Equipment": ["AMAT", "LRCX", "KLAC", "ASML", "TER"],
    "Consumer Electronics": ["AAPL", "DELL", "HPQ", "LOGI", "SONO"],
    "Internet Content & Information": ["GOOGL", "META", "PINS", "SNAP", "RDDT"],
    "Internet Retail": ["AMZN", "EBAY", "ETSY", "SHOP", "W"],
    "Electronic Gaming & Multimedia": ["MSFT", "EA", "TTWO", "NTDOY", "RBLX"],
    "Information Technology Services": ["ACN", "IBM", "CTSH", "INFY", "WIT"],
    "Technology": ["AAPL", "MSFT", "NVDA", "GOOGL", "META"],
    # ── Financials ──────────────────────────────────────────────────────────
    "Banks—Diversified": ["JPM", "BAC", "WFC", "C", "GS"],
    "Banks - Diversified": ["JPM", "BAC", "WFC", "C", "GS"],
    "Banks—Regional": ["USB", "PNC", "TFC", "FITB", "RF"],
    "Banks - Regional": ["USB", "PNC", "TFC", "FITB", "RF"],
    "Asset Management": ["BLK", "SCHW", "MS", "GS", "BX"],
    "Insurance—Life": ["MET", "PRU", "AFL", "LNC", "PFG"],
    "Insurance - Life": ["MET", "PRU", "AFL", "LNC", "PFG"],
    "Insurance—Property & Casualty": ["PGR", "ALL", "TRV", "CB", "HIG"],
    "Insurance - Property & Casualty": ["PGR", "ALL", "TRV", "CB", "HIG"],
    "Capital Markets": ["GS", "MS", "BLK", "SCHW", "MKTX"],
    "Financial Services": ["JPM", "BAC", "GS", "MS", "V"],
    "Financials": ["JPM", "BAC", "GS", "MS", "V"],
    # ── Healthcare ──────────────────────────────────────────────────────────
    "Drug Manufacturers—General": ["PFE", "MRK", "JNJ", "LLY", "ABBV"],
    "Drug Manufacturers - General": ["PFE", "MRK", "JNJ", "LLY", "ABBV"],
    "Drug Manufacturers—Specialty": ["AMGN", "GILD", "BIIB", "REGN", "VRTX"],
    "Drug Manufacturers - Specialty": ["AMGN", "GILD", "BIIB", "REGN", "VRTX"],
    "Medical Devices": ["MDT", "ABT", "SYK", "BSX", "EW"],
    "Health Care Plans": ["UNH", "CVS", "CI", "HUM", "ELV"],
    "Healthcare": ["UNH", "JNJ", "PFE", "LLY", "ABBV"],
    "Health Care": ["UNH", "JNJ", "PFE", "LLY", "ABBV"],
    "Biotechnology": ["AMGN", "GILD", "BIIB", "REGN", "VRTX"],
    # ── Consumer Defensive ──────────────────────────────────────────────────
    "Discount Stores": ["COST", "TGT", "DLTR", "DG", "BJ"],
    "Grocery Stores": ["KR", "WMT", "COST", "SFM", "CASY"],
    "Household & Personal Products": ["PG", "CL", "KMB", "CHD", "EL"],
    "Packaged Foods": ["KHC", "GIS", "CPB", "CAG", "SJM"],
    "Beverages—Non-Alcoholic": ["KO", "PEP", "MNST", "KDP", "STZ"],
    "Beverages - Non-Alcoholic": ["KO", "PEP", "MNST", "KDP", "STZ"],
    "Tobacco": ["MO", "PM", "BTI", "LO", "IMBBY"],
    "Consumer Defensive": ["WMT", "COST", "KO", "PEP", "PG"],
    # ── Consumer Cyclical ───────────────────────────────────────────────────
    "Specialty Retail": ["HD", "LOW", "ORLY", "AZO", "AAP"],
    "Home Improvement Retail": ["HD", "LOW", "FND", "FLOR", "TREX"],
    "Auto Manufacturers": ["TSLA", "F", "GM", "RIVN", "STLA"],
    "Auto Parts": ["APTV", "BWA", "LEA", "MGA", "DAN"],
    "Restaurants": ["MCD", "SBUX", "CMG", "DPZ", "YUM"],
    "Apparel Retail": ["NKE", "TJX", "ROST", "LULU", "GPS"],
    "Department Stores": ["M", "KSS", "JWN", "BURL", "DDS"],
    "Leisure": ["DIS", "MAR", "HLT", "LVS", "WYNN"],
    "Consumer Cyclical": ["AMZN", "TSLA", "HD", "NKE", "MCD"],
    # ── Energy ──────────────────────────────────────────────────────────────
    "Oil & Gas Integrated": ["XOM", "CVX", "BP", "SHEL", "TTE"],
    "Oil & Gas E&P": ["COP", "EOG", "DVN", "PXD", "MRO"],
    "Oil & Gas Refining": ["VLO", "PSX", "MPC", "DK", "PBF"],
    "Oil & Gas Midstream": ["ET", "EPD", "MMP", "WMB", "OKE"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG"],
    # ── Industrials ─────────────────────────────────────────────────────────
    "Aerospace & Defense": ["BA", "LMT", "RTX", "NOC", "GD"],
    "Railroads": ["UNP", "CSX", "NSC", "CP", "CN"],
    "Trucking": ["UPS", "FDX", "XPO", "JBHT", "ODFL"],
    "Farm & Construction Equipment": ["CAT", "DE", "CNH", "AGCO", "TITN"],
    "Industrial Distribution": ["GWW", "MSM", "FAST", "DNOW", "WSO"],
    "Electrical Equipment & Parts": ["ETN", "EMR", "ROK", "AME", "FTV"],
    "Industrials": ["CAT", "DE", "HON", "UPS", "FDX"],
    # ── Communication Services ──────────────────────────────────────────────
    "Telecom Services": ["VZ", "T", "TMUS", "LUMN", "USM"],
    "Entertainment": ["DIS", "NFLX", "PARA", "WBD", "FOXA"],
    "Communication Services": ["GOOGL", "META", "NFLX", "T", "VZ"],
    # ── Basic Materials ─────────────────────────────────────────────────────
    "Specialty Chemicals": ["LIN", "APD", "ECL", "SHW", "PPG"],
    "Gold": ["NEM", "GOLD", "AEM", "KGC", "WPM"],
    "Copper": ["FCX", "SCCO", "HBM", "ERO", "CPER"],
    "Basic Materials": ["LIN", "APD", "ECL", "SHW", "NEM"],
    # ── Real Estate ─────────────────────────────────────────────────────────
    "REIT—Industrial": ["PLD", "DRE", "STAG", "EGP", "FR"],
    "REIT - Industrial": ["PLD", "DRE", "STAG", "EGP", "FR"],
    "REIT—Office": ["BXP", "SLG", "VNO", "CXP", "HPP"],
    "REIT - Office": ["BXP", "SLG", "VNO", "CXP", "HPP"],
    "REIT—Residential": ["EQR", "AVB", "ESS", "MAA", "UDR"],
    "REIT - Residential": ["EQR", "AVB", "ESS", "MAA", "UDR"],
    "REIT—Retail": ["SPG", "O", "NNN", "KIMCO", "WPG"],
    "Real Estate": ["PLD", "AMT", "EQIX", "PSA", "DLR"],
    # ── Utilities ───────────────────────────────────────────────────────────
    "Utilities—Regulated Electric": ["NEE", "DUK", "SO", "D", "AEP"],
    "Utilities - Regulated Electric": ["NEE", "DUK", "SO", "D", "AEP"],
    "Utilities—Regulated Gas": ["SRE", "ATO", "NI", "OGE", "NW"],
    "Utilities": ["NEE", "DUK", "SO", "D", "AEP"],
    # ── Food Distribution ───────────────────────────────────────────────────
    "Food Distribution": ["SYY", "US", "PFGC", "CHEF"],
}


def _norm(s: str) -> str:
    """Normalise an industry/sector string for fuzzy matching.

    Collapses em-dashes, en-dashes, and hyphens (with optional surrounding
    whitespace) to a single hyphen, then lowercases and strips.
    """
    return re.sub(r"\s*[—–-]\s*", "-", s).lower().strip()


# Pre-build a normalised → original-key map for O(1) lookup.
_NORM_MAP: dict[str, str] = {_norm(k): k for k in _PEER_SETS}


def get_peer_tickers(ticker: str, industry: str, sector: str) -> list[str]:
    """Return up to 5 peers, never including ticker itself.

    Lookup order: exact industry → normalised industry →
                  exact sector  → normalised sector  → empty list.
    """
    ticker_up = ticker.upper()
    for key in (industry, sector):
        if not key:
            continue
        # Exact match first
        if key in _PEER_SETS:
            return [p for p in _PEER_SETS[key] if p != ticker_up][:5]
        # Normalised match (handles em-dash vs hyphen, case differences)
        norm_key = _norm(key)
        if norm_key in _NORM_MAP:
            raw_key = _NORM_MAP[norm_key]
            return [p for p in _PEER_SETS[raw_key] if p != ticker_up][:5]
    return []
