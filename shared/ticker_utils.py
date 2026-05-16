import re

_STOCK_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def is_valid_ticker_format(ticker: str) -> bool:
    return bool(_STOCK_TICKER_RE.match(ticker))


def extract_ticker(query: str) -> str:
    m = re.search(r"\(([A-Z]{1,5})\)", query)
    if m:
        return m.group(1)

    m = re.search(r"(?:for|of|about|buy|sell|invest|in)\s+\$?([A-Z]{1,5})\b", query, re.IGNORECASE)
    if m and m.group(1).isupper():
        return m.group(1)

    m = re.search(r"\$([A-Z]{1,2})\b", query)
    if m:
        return m.group(1)

    matches = re.findall(r"\b([A-Z]{3,5})\b", query)
    if matches:
        return matches[0]

    matches = re.findall(r"\b([A-Z]{2})\b", query)
    if matches:
        return matches[-1]

    return ""
