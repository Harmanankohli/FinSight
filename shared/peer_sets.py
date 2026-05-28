"""Hand-curated peer sets for sector/industry lookup.

Used by both the Market Context Agent (Sentiment rebrand) and the Quant
sector comparison node. Lookup is first by industry, then by sector.
"""

_PEER_SETS: dict[str, list[str]] = {
    # Technology
    "Software—Infrastructure":     ["MSFT", "ORCL", "NOW", "CRM", "ADBE"],
    "Software—Application":        ["CRM", "NOW", "ADBE", "INTU", "WDAY"],
    "Semiconductors":              ["NVDA", "AMD", "INTC", "AVGO", "TSM"],
    "Semiconductor Equipment":     ["AMAT", "LRCX", "KLAC", "ASML", "TER"],
    "Consumer Electronics":        ["AAPL", "DELL", "HPQ", "LOGI", "SONO"],
    "Internet Content & Information": ["GOOGL", "META", "PINS", "SNAP", "RDDT"],
    "Internet Retail":             ["AMZN", "EBAY", "ETSY", "SHOP", "W"],
    "Electronic Gaming & Multimedia": ["MSFT", "ATVI", "EA", "TTWO", "NTDOY"],
    "Information Technology Services": ["ACN", "IBM", "CTSH", "INFY", "WIT"],
    # Financials
    "Banks—Diversified":           ["JPM", "BAC", "WFC", "C", "GS"],
    "Banks—Regional":              ["USB", "PNC", "TFC", "FITB", "RF"],
    "Asset Management":            ["BLK", "SCHW", "MS", "GS", "BX"],
    "Insurance—Life":              ["MET", "PRU", "AFL", "LNC", "PFG"],
    "Insurance—Property & Casualty": ["PGR", "ALL", "TRV", "CB", "HIG"],
    "Capital Markets":             ["GS", "MS", "BLK", "SCHW", "MKTX"],
    # Healthcare
    "Drug Manufacturers—General":  ["PFE", "MRK", "JNJ", "LLY", "ABBV"],
    "Drug Manufacturers—Specialty": ["AMGN", "GILD", "BIIB", "REGN", "VRTX"],
    "Medical Devices":             ["MDT", "ABT", "SYK", "BSX", "EW"],
    "Health Care Plans":           ["UNH", "CVS", "CI", "HUM", "ELV"],
    # Energy
    "Oil & Gas Integrated":        ["XOM", "CVX", "BP", "SHEL", "TTE"],
    "Oil & Gas E&P":               ["COP", "EOG", "DVN", "PXD", "MRO"],
    "Oil & Gas Refining":          ["VLO", "PSX", "MPC", "DK", "PBF"],
    # Industrials
    "Aerospace & Defense":         ["BA", "LMT", "RTX", "NOC", "GD"],
    "Railroads":                   ["UNP", "CSX", "NSC", "CP", "CN"],
    "Auto Manufacturers":          ["TSLA", "F", "GM", "RIVN", "STLA"],
    "Auto Parts":                  ["APTV", "BWA", "LEA", "MGA", "DAN"],
    # Consumer
    "Beverages—Non-Alcoholic":     ["KO", "PEP", "MNST", "KDP", "STZ"],
    "Food Distribution":           ["SYY", "US", "PFGC", "CHEF"],
    "Specialty Retail":            ["HD", "LOW", "ORLY", "AZO", "AAP"],
    # Real Estate
    "REIT—Industrial":             ["PLD", "DRE", "STAG", "EGP", "FR"],
    "REIT—Office":                 ["BXP", "SLG", "VNO", "CXP", "HPP"],
    # Utilities
    "Utilities—Regulated Electric": ["NEE", "DUK", "SO", "D", "AEP"],
    # Telecom
    "Telecom Services":            ["VZ", "T", "TMUS", "LUMN", "USM"],
}


def get_peer_tickers(ticker: str, industry: str, sector: str) -> list[str]:
    """Return up to 5 peers, never including ticker itself.

    Lookup order: industry → sector → empty list.
    """
    for key in (industry, sector):
        if key and key in _PEER_SETS:
            peers = [p for p in _PEER_SETS[key] if p.upper() != ticker.upper()]
            return peers[:5]
    return []
