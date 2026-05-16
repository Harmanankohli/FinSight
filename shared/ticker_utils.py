import re


def extract_ticker(query: str) -> str:
    """Extract the most likely stock ticker from a natural language query.

    Uses positional heuristics rather than a fragile blacklist:

    1. Words in parentheses — *"Microsoft (MSFT)"* (strongest signal)
    2. Words after trigger prepositions — *"for TSLA"*, *"of AAPL"*
    3. Last 3-5 letter uppercase word — tickers cluster here (MSFT, TSLA, AAPL);
       common acronyms (AI, IT, GO, CEO, SEC) are rarely this length
    4. Last resort — any 2-letter uppercase word
    """
    m = re.search(r"\(([A-Z]{1,5})\)", query)
    if m:
        return m.group(1)

    m = re.search(r"(?:for|of|about)\s+([A-Z]{1,5})\b", query)
    if m:
        return m.group(1)

    matches = re.findall(r"\b([A-Z]{3,5})\b", query)
    if matches:
        return matches[-1]

    matches = re.findall(r"\b([A-Z]{2})\b", query)
    return matches[-1] if matches else ""
