# ruff: noqa: E402, E501
"""Content extraction helpers for investment report generation."""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

from shared.reports.deck_model import (
    _DEFAULT_DISCLAIMER,
    DeckData,
    ExtractionCtx,
    ParsedTable,
    Section,
)

_REPORTS_OFFLINE = os.environ.get("REPORTS_OFFLINE", "").lower() in ("1", "true", "yes")

_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z])?$")
_FINANCIAL_HEADERS = {"metric", "current", "value", "yoy change", "context"}


def classify_table(headers: list[str], ticker: str) -> str:
    non_metric = [h for h in headers if h.lower() != "metric"]
    ticker_matches = [h for h in non_metric if _TICKER_RE.match(h)]
    if len(ticker_matches) >= 2 or ticker.upper() in (h.upper() for h in non_metric):
        return "peer"
    if all(h.lower() in _FINANCIAL_HEADERS for h in headers):
        return "financial"
    return "generic"


_BLOCK_STOP = re.compile(r"\n#{1,6}\s|\n\s*\n|(?i:\b(?:bull|bear)\s+case\s*:)")
_BARE_FIGURE = re.compile(r"^\$?\s*[\d,]+(?:\.\d+)?\s*%?$")


_RAW_ANOMALY_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:T[\d:.+-]+)?\s+(?:price_spike|price_drop|high_volume|low_volume|anomaly)\s*$",
    re.IGNORECASE,
)


def _clean_item(s: str) -> str | None:
    """Single choke-point for every risk/opportunity/bullet append.

    Strips markdown, rejects items that are too short/long, contain raw
    markdown, match bare figures, are price sentences, or raw anomaly data.
    """
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"^[-•*+]\s+", "", s)
    s = s.strip()
    if not s or "\n" in s:
        return None
    if s.startswith("#") or s.startswith("|"):
        return None
    if len(s) > 200 or len(s) < 6:
        return None
    if _BARE_FIGURE.match(s):
        return None
    if re.match(r"^(?:bull|bear)\s+case\b.*\$", s, re.IGNORECASE):
        return None
    if _RAW_ANOMALY_RE.match(s):
        return None
    return s


def _case_block(text: str, label: str) -> str | None:
    """Text following '<label> case:' up to a markdown heading, blank line,
    or the opposite/next 'X case:' label (which may be on the SAME line)."""
    m = re.search(rf"(?i)\b{label}\s+case\s*:\s*", text)
    if not m:
        return None
    rest = text[m.end() :]
    stop = _BLOCK_STOP.search(rest)
    block = rest[: stop.start()] if stop else rest
    return block.strip() or None


def _case_items(text: str, label: str) -> list[str]:
    block = _case_block(text, label)
    if not block:
        return []
    if re.search(r"^\s*[-•*+]\s+", block, re.MULTILINE):
        parts = _extract_bullets(block)
    else:
        parts = re.split(r"(?<=\.)\s+", block)
    return [it for p in parts if (it := _clean_item(p)) is not None][:5]


def fit_text(text: str, w_in: float, h_in: float, start_size: int = 20) -> tuple[str, int]:
    """Find the largest font size (step 20→18→16→14) that fits text in

    (w_in, h_in) inches. Truncates at last sentence boundary with '…' below 14.
    Explicit newlines start new segments; each segment consumes ceil(len/cpl) lines.
    """
    for size in range(start_size, 10, -2):
        chars_per_line = w_in * 96 / (size * 0.55)
        lines_available = int(h_in * 72 / (size * 1.25))
        segments = text.split("\n")
        total_lines = 0
        for seg in segments:
            if not seg:
                total_lines += 1
                continue
            seg_lines = max(1, -(-len(seg) // int(chars_per_line))) if chars_per_line > 0 else 1
            total_lines += seg_lines
        if total_lines <= lines_available:
            return text, size
    # Below 14, truncate
    size = 14
    chars_per_line = w_in * 96 / (size * 0.55)
    lines_available = int(h_in * 72 / (size * 1.25))
    budget = int(chars_per_line * lines_available)
    if budget >= len(text):
        return text, size
    # Try last sentence boundary within budget
    truncated = text[:budget]
    last_period = truncated.rfind(".")
    if last_period > budget * 0.5:
        return text[: last_period + 1] + "..", size
    # No sentence boundary — hard-truncate at last word boundary
    last_space = truncated.rfind(" ")
    if last_space > 0:
        return text[:last_space] + "..", size
    return text[:budget] + "..", size


def _truncate_at_sentence(text: str, limit: int) -> str:
    """Truncate text at the last sentence boundary before *limit* characters."""
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    last_end = max(truncated.rfind(". "), truncated.rfind(".\n"), truncated.rfind(".)"))
    if last_end > limit * 0.4:
        return truncated[: last_end + 1]
    return truncated.rstrip() + "…"


def _strip_markdown(text: str) -> str:
    """Remove markdown syntax, keeping plain readable text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    text = re.sub(r"^[-|:]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*+]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_markdown_sections(text: str) -> list[Section]:
    """Split text into sections. Handles ## headers and **Bold:** headers."""
    # First try ## / ### / #### headers
    parts = re.split(r"\n#{2,6}\s+", "\n" + text)
    if len(parts) > 1:
        sections: list[Section] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            lines = part.split("\n", 1)
            title = _strip_markdown(lines[0].strip())
            body = _strip_markdown(lines[1].strip()) if len(lines) > 1 else ""
            if title and (body or title):
                sections.append(Section(title=title, body=body))
        return sections

    # Fallback: split on **Bold Header:** or "Label:" at line start followed by content
    header_pat = re.compile(
        r"^(?:\*\*(.+?)\*\*\s*:?\s*$|([A-Z][A-Za-z\s/&]+?):\s*$)",
        re.MULTILINE,
    )
    matches = list(header_pat.finditer(text))
    if not matches:
        return []

    sections = []
    for i, m in enumerate(matches):
        title = (m.group(1) or m.group(2)).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = _strip_markdown(text[start:end].strip())
        if title and len(body) > 10:
            sections.append(Section(title=title, body=body))
    return sections


def _fmt_pct(val: float | None, mult100: bool = False) -> str:
    if val is None:
        return "N/A"
    v = val * 100 if mult100 else val
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%"


def _fmt_dollar(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"${val:,.2f}"


def _confidence_reason(agent: str, score: float) -> str:
    """Generate a human-readable reason for an agent's confidence score."""
    _FACTORS = {
        "quant": "Sharpe ratio strength, DCF/Monte Carlo availability, fundamentals and technicals coverage",
        "rag": "Filing summary quality, number of cited sources",
        "market_context": "Narrative depth, signal strength, tailwind/headwind identification, macro regime clarity",
        "analytics": "Trend analysis, forecast model, statistical summary, chart generation",
    }
    factors = _FACTORS.get(agent, "Agent output completeness")
    if score >= 0.8:
        level = "High"
    elif score >= 0.5:
        level = "Moderate"
    else:
        level = "Low"
    return f"{level} — based on {factors}"


def _norm_conf(v: float | None) -> float:
    """Normalize a confidence value to 0-1 range.

    LLMs sometimes output percentages (e.g. 75) instead of fractions (0.75).
    """
    if v is None:
        return 0.0
    if v > 1.0:
        return v / 100.0
    return v


def _extract_bullets(text: str) -> list[str]:
    """Extract bullet points from markdown-like text, labeled lines, or numbered items."""
    bullets = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Markdown bullets: - * + •
        m = re.match(r"^[•\-*+]\s+(.+)", line)
        if m:
            item = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1).strip())
            if len(item) > 5:
                bullets.append(item)
            continue
        # Numbered bullets: 1. 2. etc
        m = re.match(r"^\d+[.)]\s+(.+)", line)
        if m:
            item = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1).strip())
            if len(item) > 5:
                bullets.append(item)
            continue
        # "Label: description" lines (e.g., "Macro Volatility: Sustained high VIX...")
        m = re.match(r"^([A-Z][A-Za-z\s]+):\s+(.+)", line)
        if m and len(m.group(2)) > 10:
            item = m.group(2).strip()
            item = re.sub(r"\*\*(.+?)\*\*", r"\1", item)
            bullets.append(item)
    return bullets


_ticker_cache: dict[str, tuple[str, str, str]] = {}

_EXCHANGE_MAP = {
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
    "NYQ": "NYSE",
    "NYS": "NYSE",
    "PCX": "NYSE ARCA",
    "BTS": "CBOE",
    "LSE": "LSE",
    "TYO": "TSE",
}


def _resolve_ticker_info(
    ticker: str, text: str, company_info: dict | None = None
) -> tuple[str, str, str]:
    """Return (company_name, sector, exchange).

    Priority: company_info arg → yfinance (unless REPORTS_OFFLINE) → regex → ticker symbol.
    """
    ticker = ticker.upper()
    if ticker in _ticker_cache:
        return _ticker_cache[ticker]
    if company_info:
        name = company_info.get("name") or company_info.get("longName") or ""
        sector = company_info.get("sector") or ""
        exchange = company_info.get("exchange") or ""
        if name:
            _ticker_cache[ticker] = (name, sector, exchange)
            return _ticker_cache[ticker]
    if not _REPORTS_OFFLINE:
        try:
            import yfinance as yf

            info = yf.Ticker(ticker).info
            name = info.get("longName") or info.get("shortName") or ""
            sector = info.get("sector") or ""
            raw_exchange = info.get("exchange") or ""
            exchange = _EXCHANGE_MAP.get(raw_exchange, raw_exchange)
            if name:
                _ticker_cache[ticker] = (name, sector, exchange)
                return _ticker_cache[ticker]
        except Exception:
            logger.debug("yfinance ticker info lookup failed for report enrichment")
    for pat in [
        r"(?:for|about|of)\s+([A-Z][A-Za-z\s&.]+?)\s*\(" + re.escape(ticker) + r"\)",
        r"([A-Z][A-Za-z\s&.]+?)\s*\(" + re.escape(ticker) + r"\)",
    ]:
        m = re.search(pat, text)
        if m:
            _ticker_cache[ticker] = (m.group(1).strip(), "", "")
            return _ticker_cache[ticker]
    return (ticker, "", "")


def _parse_markdown_tables(text: str) -> tuple[str, list[dict], list[ParsedTable]]:
    """Extract markdown tables into structured dicts.

    Returns (cleaned_text, flat_rows, structured_tables).
    flat_rows kept for back-compat until R.4; structured_tables used for classification.
    """
    table_pattern = re.compile(r"((?:^\|.+\|\s*\n)+)", re.MULTILINE)
    parsed_tables: list[list[dict]] = []
    structured_tables: list[ParsedTable] = []
    for match in table_pattern.finditer(text):
        block = match.group(1)
        lines = [ln.strip() for ln in block.strip().split("\n") if ln.strip()]
        data_lines = [ln for ln in lines if not re.match(r"^\|(?:[\s\-:]+\|)+$", ln)]
        if len(data_lines) < 2:
            continue
        headers = [c.strip() for c in data_lines[0].split("|")[1:-1]]
        rows = []
        for line in data_lines[1:]:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells)))
        if rows:
            parsed_tables.append(rows)
            structured_tables.append(ParsedTable(headers=headers, rows=rows))
    cleaned = table_pattern.sub("", text)
    return cleaned, [row for table in parsed_tables for row in table], structured_tables


# ── Module-level constants for staged extraction ──────────────────────────
_METRIC_PATTERNS: list[tuple[str, list[str], bool]] = [
    (
        "Revenue Growth",
        [
            r"revenue\s+growth\s*[\(:]\s*([+-]?\d+\.?\d*)\s*%",
            r"revenue\s+growth[:\s]+([+-]?\d+\.?\d*)\s*%",
            r"revenue\s+growth\s+(?:of\s+)?([+-]?\d+\.?\d*)\s*%",
        ],
        True,
    ),
    (
        "ROE",
        [
            r"ROE\s*[\(:]\s*([+-]?\d+\.?\d*)\s*%",
            r"(?:ROE|return\s+on\s+equity)[:\s]+([+-]?\d+\.?\d*)\s*%",
            r"ROE\s+of\s+([+-]?\d+\.?\d*)\s*%",
        ],
        True,
    ),
    (
        "Operating Margin",
        [
            r"operating\s+margin\s*[\(:]\s*([+-]?\d+\.?\d*)\s*%",
            r"operating\s+margin[:\s]+([+-]?\d+\.?\d*)\s*%",
            r"operating\s+margin\s+(?:stands?\s+at|of|at)\s+([+-]?\d+\.?\d*)\s*%",
        ],
        True,
    ),
    (
        "P/E Ratio",
        [
            r"P/?E\s*[\(:=]\s*([+-]?\d+\.?\d*)",
            r"P/?E\s+(?:ratio\s*)?[:\s]+([+-]?\d+\.?\d*)",
            r"trailing\s+PE\s*[\(:]\s*([+-]?\d+\.?\d*)",
            r"(?:trailing\s+)?P/?E\s+of\s+([+-]?\d+\.?\d*)",
        ],
        False,
    ),
    (
        "Beta",
        [
            r"[Bb]eta\s*[\(:=]\s*([+-]?\d+\.?\d*)",
            r"[Bb]eta[:\s]+([+-]?\d+\.?\d*)",
            r"[Bb]eta\s+of\s+([+-]?\d+\.?\d*)",
        ],
        False,
    ),
    (
        "Sharpe Ratio",
        [
            r"[Ss]harpe\s+[Rr]atio\s*[\(:=]\s*([+-]?\d+\.?\d*)",
            r"[Ss]harpe\s+[Rr]atio[:\s]+([+-]?\d+\.?\d*)",
            r"[Ss]harpe\s+[Rr]atio\s+of\s+([+-]?\d+\.?\d*)",
        ],
        False,
    ),
    (
        "RSI",
        [
            r"RSI\s*[\(:=]\s*([+-]?\d+\.?\d*)",
            r"RSI\s+of\s+([+-]?\d+\.?\d*)",
        ],
        False,
    ),
    (
        "Volatility",
        [
            r"(?:annual\s+)?volatility\s*[\(:]\s*([+-]?\d+\.?\d*)\s*%",
            r"(?:annual\s+)?volatility[:\s]+([+-]?\d+\.?\d*)\s*%",
            r"(?:annual\s+)?volatility\s+of\s+([+-]?\d+\.?\d*)\s*%",
        ],
        True,
    ),
    (
        "Debt/Equity",
        [
            r"(?:debt[/\\]equity|D/E)\s*[\(:=]\s*([+-]?\d+\.?\d*)",
            r"(?:debt[/\\]equity|D/E)\s+of\s+([+-]?\d+\.?\d*)",
        ],
        False,
    ),
    (
        "Dividend Yield",
        [
            r"dividend\s+yield\s*[\(:=]\s*([+-]?\d+\.?\d*)\s*%",
            r"dividend\s+yield\s+of\s+([+-]?\d+\.?\d*)\s*%",
        ],
        True,
    ),
    (
        "EPS",
        [
            r"(?:diluted\s+)?EPS\s*[\(:=]\s*\$?\s*([+-]?\d+\.?\d*)",
            r"(?:diluted\s+)?EPS\s+of\s+\$?\s*([+-]?\d+\.?\d*)",
        ],
        False,
    ),
    (
        "Current Ratio",
        [
            r"current\s+ratio\s*[\(:=]\s*([+-]?\d+\.?\d*)",
            r"current\s+ratio\s+of\s+([+-]?\d+\.?\d*)",
        ],
        False,
    ),
    (
        "Net Margin",
        [
            r"net\s+(?:profit\s+)?margin\s*[\(:=]\s*([+-]?\d+\.?\d*)\s*%",
            r"net\s+(?:profit\s+)?margin\s+of\s+([+-]?\d+\.?\d*)\s*%",
        ],
        True,
    ),
]

_KPI_CONTEXT = {
    "Revenue Growth": "YoY",
    "ROE": "Return on Equity",
    "Operating Margin": "Profitability",
    "P/E Ratio": "Valuation",
    "Beta": "Mkt sensitivity",
    "RSI": "Momentum",
    "Sharpe Ratio": "Risk-adj return",
    "Volatility": "Annualized",
    "Debt/Equity": "Leverage",
    "Dividend Yield": "Income",
    "EPS": "Earnings per share",
    "Current Ratio": "Liquidity",
    "Net Margin": "Profitability",
}

_KPI_PRIORITY = [
    "Revenue Growth",
    "ROE",
    "Operating Margin",
    "P/E Ratio",
    "Sharpe Ratio",
    "Beta",
    "Dividend Yield",
    "RSI",
    "Volatility",
    "EPS",
    "Net Margin",
    "Debt/Equity",
    "Current Ratio",
]

_FIN_CONTEXT = {
    "Revenue Growth": "Year-over-year",
    "ROE": "Return on Equity",
    "Operating Margin": "Profitability",
    "P/E Ratio": "Price / Earnings",
    "Beta": "Market sensitivity",
    "Sharpe Ratio": "Risk-adjusted return",
    "RSI": "Momentum (14-day)",
    "Volatility": "Annualized",
    "Debt/Equity": "Leverage ratio",
    "Dividend Yield": "Annual yield",
    "EPS": "Earnings per share",
    "Current Ratio": "Liquidity",
    "Net Margin": "Net profitability",
}

_FINANCIAL_ABBREVS = {
    "DCF",
    "MACD",
    "VIX",
    "DXY",
    "SEC",
    "LLM",
    "WACC",
    "RSI",
    "EPS",
    "EBITDA",
    "IPO",
    "CEO",
    "CFO",
    "ETF",
    "GDP",
    "CPI",
    "FOMC",
    "YOY",
    "QOQ",
    "MOM",
    "ADK",
    "SSE",
    "API",
    "YTD",
}


def _stage_tables(ctx: ExtractionCtx) -> None:
    """Classify and extract table data: financials and peers."""
    if not ctx.tables:
        return
    for parsed_table in ctx.tables:
        classification = classify_table(parsed_table.headers, ctx.data.ticker)
        if classification == "financial":
            for row in parsed_table.rows:
                metric = row.get("Metric") or row.get("metric") or ""
                for val_key in ("Current", "Value", "current", "value"):
                    if val_key in row and metric:
                        ctx.data.financials.append(
                            (metric, row[val_key], row.get("YoY Change", row.get("Context", "")))
                        )
                        break
        elif classification == "peer":
            valid_peers = [
                h
                for h in parsed_table.headers
                if h.lower() != "metric" and h.upper() != ctx.data.ticker and _TICKER_RE.match(h)
            ]
            if not valid_peers:
                continue
            for pn in valid_peers[:2]:
                if pn not in ctx.data.peer_names:
                    ctx.data.peer_names.append(pn)
            for row in parsed_table.rows:
                metric = row.get("Metric") or row.get("metric") or ""
                if not metric:
                    continue
                peer_row = {"metric": metric}
                ticker_val = row.get(ctx.data.ticker, "")
                if ticker_val:
                    peer_row["col0"] = ticker_val
                for ci, pn in enumerate(valid_peers[:2]):
                    if pn in row:
                        peer_row[f"col{ci + 1}"] = row[pn]
                if len(peer_row) > 1:
                    ctx.data.peers.append(peer_row)


def _stage_metrics(ctx: ExtractionCtx) -> None:
    """Extract numeric metrics from text into extracted_metrics dict + KPI chips."""
    for label, pats, is_pct in _METRIC_PATTERNS:
        for pat in pats:
            m = re.search(pat, ctx.text, re.IGNORECASE)
            if m:
                val = m.group(1)
                ctx.extracted_metrics[label] = f"{val}%" if is_pct else val
                break
    selected = [lbl for lbl in _KPI_PRIORITY if lbl in ctx.extracted_metrics][:4]
    for lbl in ctx.extracted_metrics:
        if lbl not in selected and len(selected) < 4:
            selected.append(lbl)
    for label in selected:
        val = ctx.extracted_metrics[label]
        ctx.data.kpi_chips.append(
            {
                "label": label,
                "value": val,
                "context": _KPI_CONTEXT.get(label, ""),
                "positive": not val.startswith("-"),
            }
        )


def _stage_scenarios(ctx: ExtractionCtx) -> None:
    """Extract price targets, DCF, Monte Carlo, bull/bear prices, current price."""
    text = ctx.text
    d = ctx.data
    target_pats = [
        r"(?:avg\.?\s+)?(?:price\s+)?target\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"(?:target\s+price|price\s+target|median\s+target)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"(?:average\s+)?target\s+price\s+of\s+\$\s*([\d,]+\.?\d*)",
    ]
    for pat in target_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            d.valuation_table.append(("Analyst Price Target", f"${m.group(1)}"))
            d.scenarios["base"] = f"${m.group(1)}"
            break
    upside_pats = [
        r"(\d+\.?\d*)\s*%\s+upside\s+potential",
        r"upside\s+potential\s*[\(:]\s*(\d+\.?\d*)\s*%",
        r"expected\s+(?:upside|return)\s*[\(:]\s*(\d+\.?\d*)\s*%",
        r"expected\s+(?:upside|return)\s*[=:]\s*([+-]?\d+\.?\d*)\s*%",
        r"(?:analyst|consensus)\s+upside\s*[\(:]\s*(\d+\.?\d*)\s*%",
        r"median\s+upside[:\s]+([+-]?\d+\.?\d*)\s*%",
        r"implying\s+(?:a\s+)?(\d+\.?\d*)\s*%\s+upside",
    ]
    for pat in upside_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1)
            prefix = "" if val.startswith(("+", "-")) else "+"
            d.valuation_table.append(("Expected Upside", f"{prefix}{val}%"))
            break
    dcf_pats = [
        r"DCF\s+(?:fair\s+value|intrinsic\s+value)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"(?:fair\s+value|intrinsic\s+value)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"intrinsic\s+value\s+of\s+\$\s*([\d,]+\.?\d*)",
    ]
    for pat in dcf_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            d.valuation_table.append(("DCF Fair Value", f"${m.group(1)}"))
            d.scenarios["dcf"] = f"${m.group(1)}"
            break
    bull_m = re.search(r"bull\s+case[:\s]*\$\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if bull_m:
        d.valuation_table.append(("95th Percentile Target", f"${bull_m.group(1)}"))
        d.scenarios["bull"] = f"${bull_m.group(1)}"
    bear_m = re.search(r"bear\s+case[:\s]*\$\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if bear_m:
        d.valuation_table.append(("5th Percentile Target", f"${bear_m.group(1)}"))
        d.scenarios["bear"] = f"${bear_m.group(1)}"
    # Percentile-based Monte Carlo labels (preferred over bear/bull terminology)
    p90_m = re.search(
        r"(?:90th|95th)\s+percentile[:\s]*\$\s*([\d,]+\.?\d*)", text, re.IGNORECASE
    )
    if p90_m and not bull_m:
        d.valuation_table.append(("95th Percentile Target", f"${p90_m.group(1)}"))
        d.scenarios["bull"] = f"${p90_m.group(1)}"
    p10_m = re.search(
        r"(?:5th|10th)\s+percentile[:\s]*\$\s*([\d,]+\.?\d*)", text, re.IGNORECASE
    )
    if p10_m and not bear_m:
        d.valuation_table.append(("5th Percentile Target", f"${p10_m.group(1)}"))
        d.scenarios["bear"] = f"${p10_m.group(1)}"
    p50_m = re.search(
        r"median(?:\s+price)?[:\s]*\$\s*([\d,]+\.?\d*)", text, re.IGNORECASE
    )
    if p50_m:
        d.valuation_table.append(("Median (50th Pct) Target", f"${p50_m.group(1)}"))
        d.scenarios["base"] = d.scenarios.get("base") or f"${p50_m.group(1)}"
    mc_p90_pats = [
        r"p90\s*[=:]\s*\$\s*([\d,]+\.?\d*)",
        r"90th\s+percentile\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"bull(?:ish)?\s+(?:scenario|outcome)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
    ]
    if "bull" not in d.scenarios:
        for pat in mc_p90_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                d.valuation_table.append(("Bull Case (p90)", f"${m.group(1)}"))
                d.scenarios["bull"] = f"${m.group(1)}"
                break
    mc_p50_pats = [
        r"p50\s*[=:]\s*\$\s*([\d,]+\.?\d*)",
        r"50th\s+percentile\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"median\s+(?:outcome|price|target|value)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
    ]
    if "base" not in d.scenarios:
        for pat in mc_p50_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                d.valuation_table.append(("Base Case (p50)", f"${m.group(1)}"))
                d.scenarios["base"] = f"${m.group(1)}"
                break
    mc_p10_pats = [
        r"p10\s*[=:]\s*\$\s*([\d,]+\.?\d*)",
        r"10th\s+percentile\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"bear(?:ish)?\s+(?:scenario|outcome)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
    ]
    if "bear" not in d.scenarios:
        for pat in mc_p10_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                d.valuation_table.append(("Bear Case (p10)", f"${m.group(1)}"))
                d.scenarios["bear"] = f"${m.group(1)}"
                break
    prob_pats = [
        r"prob(?:ability)?\s+(?:of\s+)?(?:positive\s+)?(?:return|profit)\s*[:\s]+(\d+\.?\d*)\s*%",
        r"prob(?:ability)?\s+(?:of\s+)?(?:positive\s+)?(?:return|profit)\s*[\(]\s*(\d+\.?\d*)\s*%",
    ]
    for pat in prob_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            d.valuation_table.append(("Prob. of Positive Return", f"{m.group(1)}%"))
            break
    cvar_m = re.search(r"CVaR\s*[\(:=]\s*(-?\d+\.?\d*)\s*%", text, re.IGNORECASE)
    if cvar_m:
        d.valuation_table.append(("CVaR (95%)", f"{cvar_m.group(1)}%"))
    price_pats = [
        r"current\s+price\s+of\s+\$\s*([\d,]+\.?\d*)",
        r"current\s+(?:stock\s+)?price\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"(?:trading|priced?)\s+at\s+\$\s*([\d,]+\.?\d*)",
    ]
    for pat in price_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            d.valuation_table.insert(0, ("Current Price", f"${m.group(1)}"))
            break


def _stage_financials_scorecard(ctx: ExtractionCtx) -> None:
    """Build financials table from extracted metrics and scorecard from text."""
    d = ctx.data
    text = ctx.text
    existing_fin_metrics = {row[0] for row in d.financials}
    for label in _KPI_PRIORITY:
        if label in ctx.extracted_metrics and label not in existing_fin_metrics:
            d.financials.append((label, ctx.extracted_metrics[label], _FIN_CONTEXT.get(label, "")))
    _STRONG_MAP = {
        "strong": "strong",
        "solid": "strong",
        "robust": "strong",
        "moderate": "moderate",
        "weak": "expensive",
        "poor": "expensive",
    }
    _VALUATION_MAP = {
        "expensive": "expensive",
        "rich": "expensive",
        "premium": "expensive",
        "extreme": "expensive",
        "high": "expensive",
        "elevated": "expensive",
        "cheap": "strong",
        "undervalued": "strong",
        "fair": "moderate",
        "overvalued": "expensive",
    }
    _RISK_MAP = {
        "low": "strong",
        "moderate": "moderate",
        "high": "expensive",
        "elevated": "expensive",
        "significant": "expensive",
    }
    _ANALYST_MAP = {
        "strong buy": "strong",
        "buy": "strong",
        "outperform": "strong",
        "overweight": "strong",
        "hold": "bullish",
        "neutral": "moderate",
        "sell": "expensive",
        "underweight": "expensive",
    }
    scorecard_entries: list[tuple[str, list[str], dict[str, str]]] = [
        (
            "Fundamentals",
            [
                r"(strong|weak|moderate|solid|robust)\s+(?:revenue\s+growth|fundamentals?|earnings)",
                r"fundamental[s]?\s*[:\s]?\s*(strong|weak|moderate|solid|robust)",
            ],
            _STRONG_MAP,
        ),
        (
            "Technical Outlook",
            [
                r"(?:MACD|macd)\s+(?:indicator\s+)?(?:is\s+)?(bullish|bearish)",
                r"(sideways)\s+trend",
                r"(?:technical[s]?|trend)\s*[:\s]?\s*(bullish|bearish|neutral|mixed)",
                r"(bullish|bearish|neutral|mixed)\s+(?:signals?|technicals?|momentum)",
                r"(?<!lack of a )(?<!lack of )(strong\s+uptrend|uptrend|downtrend|golden\s+cross)\b",  # noqa: E501
            ],
            {
                "bullish": "bullish",
                "strong uptrend": "bullish",
                "uptrend": "bullish",
                "golden cross": "bullish",
                "bearish": "expensive",
                "downtrend": "expensive",
                "neutral": "moderate",
                "mixed": "moderate",
                "sideways": "moderate",
            },
        ),
        (
            "Valuation",
            [
                r"(extreme|high|elevated|premium|rich|expensive)\s+valuation",
                r"valuation\s*[:\s]?\s*(expensive|cheap|fair|rich|premium|undervalued|overvalued|extreme|high|elevated)",
                r"(?:stock|company|it)\s+(?:may\s+be|is|appears?)\s+(overvalued|undervalued)",
                r"(?:may\s+be|is)\s+(overvalued|undervalued)\s+relative",
            ],
            _VALUATION_MAP,
        ),
        (
            "Risk Profile",
            [
                r"(significant|elevated|high)\s+(?:tail\s+)?risk",
                r"(?:risk|volatility|tail\s+risk)\s*[:\s]?\s*(low|moderate|high|elevated|significant)",
            ],
            _RISK_MAP,
        ),
        (
            "Profitability",
            [
                r"(robust|strong|high)\s+operating\s+margin",
                r"operating\s+margin\s*[\(:]\s*\d+",
                r"(?:profitability|margins?)\s*[:\s]?\s*(strong|weak|moderate|robust|high|improving)",
            ],
            {
                "strong": "strong",
                "robust": "strong",
                "high": "strong",
                "improving": "bullish",
                "moderate": "moderate",
                "weak": "expensive",
            },
        ),
        (
            "Momentum",
            [
                r"RSI\s*[\(:=]\s*(\d+)",
                r"RSI\s+of\s+(\d+\.?\d*)",
            ],
            {"overbought": "expensive", "oversold": "strong"},
        ),
        (
            "Analyst Sentiment",
            [
                r"""consensus\s+[\"']?(strong\s+buy|buy|hold|sell|outperform|overweight)[\"']?\s+recommend""",
                r"(?:analyst[s]?\s+(?:consensus|recommend|sentiment)|consensus)\s*[:\s]?\s*(strong\s+buy|buy|hold|sell|outperform|overweight|underweight|neutral)",
                r"""recommend[s]?\s+[\"']?(strong\s+buy|buy|hold|sell|outperform|overweight)[\"']?""",
            ],
            _ANALYST_MAP,
        ),
    ]
    for dim, pats, mapping in scorecard_entries:
        for pat in pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                word = m.group(1).lower() if m.lastindex else ""
                badge = mapping.get(word, "moderate")
                if dim == "Profitability" and m.group(0) and "margin" in m.group(0).lower():
                    margin_m = re.search(
                        r"operating\s+margin\s*[\(:]\s*(\d+\.?\d*)", text, re.IGNORECASE
                    )
                    if margin_m and float(margin_m.group(1)) > 20:
                        word, badge = "strong", "strong"
                    elif margin_m and float(margin_m.group(1)) > 10:
                        word, badge = "moderate", "moderate"
                elif dim == "Momentum":
                    rsi_val = float(m.group(1))
                    if rsi_val > 70:
                        word, badge = "Overbought", "expensive"
                    elif rsi_val >= 50:
                        word, badge = "Bullish", "bullish"
                    elif rsi_val >= 30:
                        word, badge = "Neutral", "moderate"
                    else:
                        word, badge = "Oversold", "strong"
                d.scorecard.append((dim, word.capitalize(), badge))
                break
    d.scorecard.append(
        (
            "Recommendation",
            d.recommendation,
            "strong"
            if d.recommendation == "BUY"
            else "bullish"
            if d.recommendation == "HOLD"
            else "expensive",
        )
    )


def _stage_risks_opportunities(ctx: ExtractionCtx) -> None:
    """Extract risks and opportunities from sections, labeled blocks, and bull/bear cases."""
    d = ctx.data
    text = ctx.text
    sections = ctx.sections
    for sec in sections:
        lower_title = sec.title.lower()
        if any(
            k in lower_title
            for k in ("risk", "threat", "concern", "challenge", "bearish", "headwind")
        ):
            d.risks.extend(filter(None, (_clean_item(b) for b in _extract_bullets(sec.body)[:5])))
        elif any(
            k in lower_title
            for k in (
                "opportunit",
                "catalyst",
                "growth",
                "strength",
                "upside",
                "bullish",
                "tailwind",
            )
        ):
            d.opportunities.extend(
                filter(None, (_clean_item(b) for b in _extract_bullets(sec.body)[:5]))
            )
    if not d.risks:
        risk_block = re.search(
            r"(?:key\s+risks?\s*(?:to\s+monitor)?|risk\s+factors?)\s*:\s*\n((?:.*\n)*?)(?:\n\n|\nnext\s+step|\Z)",
            text,
            re.IGNORECASE,
        )
        if risk_block:
            d.risks = list(
                filter(None, (_clean_item(b) for b in _extract_bullets(risk_block.group(1))[:5]))
            )
    if not d.risks:
        inline_risk = re.search(
            r"key\s+risks?\s*:\s*(.+?)(?:\.\s*$|\n\n|\n[A-Z])",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if inline_risk:
            items = re.split(r",\s*(?:and\s+)?", inline_risk.group(1))
            d.risks = list(
                filter(
                    None, (_clean_item(i.strip().rstrip(".")) for i in items if len(i.strip()) > 5)
                )
            )[:5]
    if not d.opportunities:
        d.opportunities = _case_items(text, "bull")
    if not d.risks:
        d.risks = _case_items(text, "bear")
    if not d.opportunities:
        opp_block = re.search(
            r"(?:growth\s+(?:opportunities|drivers)|catalysts?)\s*:\s*\n((?:[\s\S]*?)(?=\n\n|\n[A-Z][a-z]|\Z))",
            text,
            re.IGNORECASE,
        )
        if opp_block:
            d.opportunities = list(
                filter(None, (_clean_item(b) for b in _extract_bullets(opp_block.group(1))[:5]))
            )

    def _extract_labeled_block(label_pat: str) -> list[str]:
        block_m = re.search(
            rf"\*\*{label_pat}\s*:\*\*\s*\n((?:\s+[-•*+].*\n?)+)",
            text,
            re.IGNORECASE,
        )
        if block_m:
            return list(
                filter(None, (_clean_item(b) for b in _extract_bullets(block_m.group(1))[:5]))
            )
        inline_m = re.search(rf"\*\*{label_pat}\s*:\*\*\s*(.+)", text, re.IGNORECASE)
        if inline_m:
            items = re.split(r",\s*(?:and\s+)?", inline_m.group(1))
            return list(
                filter(
                    None, (_clean_item(i.strip().rstrip(".")) for i in items if len(i.strip()) > 5)
                )
            )[:5]
        return []

    if not d.risks:
        for lbl in (r"bearish\s+signals?", r"headwinds?"):
            d.risks.extend(_extract_labeled_block(lbl))
    if not d.opportunities:
        for lbl in (r"bullish\s+signals?", r"tailwinds?"):
            d.opportunities.extend(_extract_labeled_block(lbl))
    if not d.risks:
        d.risks = ["Market volatility", "Regulatory changes"]
    else:
        d.risks_extracted = True
    if not d.opportunities:
        d.opportunities = ["Strong operational execution", "Market positioning"]
    else:
        d.opportunities_extracted = True


def _stage_peers(ctx: ExtractionCtx) -> None:
    """Extract peer tickers from narrative text when not already found from tables."""
    d = ctx.data
    text = ctx.text
    if d.peer_names:
        return
    peer_pattern = re.compile(r"([A-Z][A-Za-z\s]+?)\s*\(([A-Z]{2,5})\)")
    for m in peer_pattern.finditer(text):
        sym = m.group(2)
        if sym != d.ticker and sym not in d.peer_names and sym not in _FINANCIAL_ABBREVS:
            d.peer_names.append(sym)
            if len(d.peer_names) >= 2:
                break
    if not d.peer_names:
        reverse_pattern = re.compile(r"\b([A-Z]{2,5})\s*\(([A-Z][A-Za-z\s&.]+?)\)")
        for m in reverse_pattern.finditer(text):
            sym = m.group(1)
            if sym != d.ticker and sym not in d.peer_names and sym not in _FINANCIAL_ABBREVS:
                d.peer_names.append(sym)
                if len(d.peer_names) >= 2:
                    break
    if not d.peer_names:
        _bare_peer_pats = [
            r"(?:peers?\s+(?:like|such\s+as|including)|compared?\s+to)\s+([A-Z]{2,5})",
            r"(?:pressure|competition)\s+from\s+([A-Z]{2,5})",
            r"(?:vs\.?|versus)\s+([A-Z]{2,5})",
            r"for\s+([A-Z]{2,5})\s+vs\.",
        ]
        for pat in _bare_peer_pats:
            for m in re.finditer(pat, text):
                sym = m.group(1)
                if sym != d.ticker and sym not in d.peer_names and sym not in _FINANCIAL_ABBREVS:
                    d.peer_names.append(sym)
                    if len(d.peer_names) >= 2:
                        break
            if len(d.peer_names) >= 2:
                break


def _stage_executive_summary(ctx: ExtractionCtx) -> None:
    """Build a synthesized executive summary from extracted data."""
    d = ctx.data
    text = ctx.text
    sections = ctx.sections
    summary_parts: list[str] = []
    fund_metrics = []
    for label in ("Revenue Growth", "ROE", "Operating Margin", "P/E Ratio"):
        if label in ctx.extracted_metrics:
            suffix = "x" if label == "P/E Ratio" else ""
            display_label = label if label in ("ROE", "P/E Ratio") else label.lower()
            fund_metrics.append(f"{display_label} of {ctx.extracted_metrics[label]}{suffix}")
    if fund_metrics:
        summary_parts.append(f"{d.company_name} demonstrates {', '.join(fund_metrics)}")
    dcf_val = next(
        (v for lbl, v in d.valuation_table if "dcf" in lbl.lower() or "intrinsic" in lbl.lower()),
        None,
    )
    cur_price = next((v for lbl, v in d.valuation_table if "current price" in lbl.lower()), None)
    if dcf_val and cur_price:
        summary_parts.append(
            f"DCF analysis estimates intrinsic value at {dcf_val} versus the current trading price of {cur_price}"  # noqa: E501
        )
    elif dcf_val:
        summary_parts.append(f"DCF analysis estimates intrinsic value at {dcf_val}")
    for vlabel, vval in d.valuation_table:
        if "target" in vlabel.lower():
            upside_str = ""
            for ul, uv in d.valuation_table:
                if "upside" in ul.lower():
                    upside_str = f", implying {uv} upside"
                    break
            summary_parts.append(f"Analyst consensus targets {vval}{upside_str}")
            break
    for dim, rating, _ in d.scorecard:
        if dim == "Technical Outlook":
            summary_parts.append(f"Technical outlook is {rating.lower()}")
            break
    if d.opportunities and d.opportunities[0] not in (
        "Strong operational execution",
        "Market positioning",
    ):
        summary_parts.append(d.opportunities[0].rstrip("."))
    if d.risks and d.risks[0] != "Market volatility":
        first_risk = re.split(r"(?<!\d)\.(?!\d)", d.risks[0])[0]
        summary_parts.append(f"Key risk: {first_risk}")
    if summary_parts:
        d.executive_summary = ". ".join(summary_parts) + "."
        if len(d.executive_summary) < 300:
            for sec in sections:
                lt = sec.title.lower()
                if any(
                    k in lt
                    for k in (
                        "rationale",
                        "thesis",
                        "investment thesis",
                        "summary",
                        "synthesis",
                        "outlook",
                    )
                ):
                    narrative = sec.body.strip()
                    if len(narrative) > 50:
                        d.executive_summary = d.executive_summary.rstrip(".") + ". " + narrative
                        break
        d.executive_summary = d.executive_summary[:4000]
    else:
        bull = re.search(
            r"(?:bull\s+case|synthesis|investment\s+thesis)\s*:\s*(.+?)(?:\n(?:bear\s+case|confidence|key\s+risk)|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if bull:
            raw = bull.group(1).strip()
            first_sent = re.split(r"(?<=\.)\s+", raw)[0]
            d.executive_summary = _strip_markdown(first_sent[:600])
        else:
            lines = text.strip().split("\n")
            substance = []
            for line in lines:
                line = line.strip()
                if (
                    not line
                    or line.startswith("Here is")
                    or line.startswith("Investment Recommendation")
                    or line.startswith("Confidence Score")
                ):
                    continue
                substance.append(line)
                if len(" ".join(substance)) > 400:
                    break
            d.executive_summary = _strip_markdown(" ".join(substance)[:600])


# Ordered stage list — each stage operates on ExtractionCtx in place.
STAGES: list = [
    _stage_tables,
    _stage_metrics,
    _stage_scenarios,
    _stage_financials_scorecard,
    _stage_risks_opportunities,
    _stage_peers,
    _stage_executive_summary,
]


def _enrich_from_markdown(
    data: DeckData,
    text: str,
    sections: list[Section],
    table_rows: list[dict] = None,
    tables: list[ParsedTable] = None,
) -> None:
    """Run the staged extraction pipeline over text into data."""
    ctx = ExtractionCtx(
        data=data,
        text=text,
        sections=sections,
        tables=tables or [],
    )
    for stage in STAGES:
        logger.debug("Extraction stage: %s", stage.__name__)
        stage(ctx)
    logger.info(
        "Extraction complete: ticker=%s, kpis=%d, risks=%d, opps=%d, peers=%d, scorecard=%d",
        data.ticker,
        len(data.kpi_chips),
        len(data.risks),
        len(data.opportunities),
        len(data.peer_names),
        len(data.scorecard),
    )


def _safe_parse(val):
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _populate_from_agent_outputs(data: DeckData, brief_data: dict, response_text: str) -> None:
    """Populate DeckData from structured agent output dicts stored in brief_json."""
    quant = _safe_parse(brief_data.get("quant_response"))
    rag = _safe_parse(brief_data.get("rag_response"))
    sentiment = _safe_parse(brief_data.get("sentiment_response"))
    analytics = _safe_parse(brief_data.get("analytics_response"))
    reviewer = _safe_parse(brief_data.get("reviewer_response"))
    logger.info(
        "Populating from agent outputs: quant=%s, rag=%s, sentiment=%s, analytics=%s, reviewer=%s",
        bool(quant),
        bool(rag),
        bool(sentiment),
        bool(analytics),
        bool(reviewer),
    )

    # ── Quant response ─────────────────────────────────────────────────────
    if quant:
        metrics = quant.get("metrics") or {}
        fundamentals = quant.get("fundamentals") or {}

        # Only use metrics-derived KPIs when they have real (non-zero) values;
        # the quant agent zeros these out when historical price data is unavailable.
        if metrics.get("sharpe_ratio"):
            sr = metrics["sharpe_ratio"]
            data.kpi_chips.append(
                {
                    "label": "Sharpe Ratio",
                    "value": f"{sr:.2f}",
                    "context": "Risk-adjusted return",
                    "positive": sr > 0,
                }
            )
            data.financials.append(("Sharpe Ratio", f"{sr:.2f}", "Risk-Adjusted"))
        if metrics.get("annual_volatility"):
            av = metrics["annual_volatility"]
            data.kpi_chips.append(
                {
                    "label": "Annual Volatility",
                    "value": _fmt_pct(av, True),
                    "context": "Annualized",
                    "positive": av < 0.25,
                }
            )
            data.financials.append(("Volatility", _fmt_pct(av, True), "Annualized"))
        if metrics.get("beta"):
            data.kpi_chips.append(
                {
                    "label": "Beta",
                    "value": f"{metrics['beta']:.2f}",
                    "context": "Mkt sensitivity",
                    "positive": metrics["beta"] < 1.0,
                }
            )
            data.financials.append(("Beta", f"{metrics['beta']:.2f}", "vs. S&P 500"))
        if metrics.get("var_95_daily"):
            data.financials.append(
                ("VaR (95%, daily)", _fmt_pct(metrics["var_95_daily"], True), "Daily")
            )

        # Fall back to fundamentals for KPI chips when metrics are zeroed
        if not data.kpi_chips and fundamentals:
            _CHIP_FUNDAMENTALS = [
                ("trailing_pe", "P/E Ratio", False, "Valuation"),
                ("roe", "ROE", True, "Return on Equity"),
                ("operating_margin", "Op. Margin", True, "Profitability"),
                ("revenue_growth", "Rev. Growth", True, "Year-over-year"),
            ]
            for key, label, is_pct, ctx in _CHIP_FUNDAMENTALS:
                val = fundamentals.get(key)
                if val is not None and len(data.kpi_chips) < 4:
                    formatted = _fmt_pct(val, is_pct) if is_pct else f"{val:.2f}"
                    positive = val > 0 if is_pct else True
                    data.kpi_chips.append(
                        {"label": label, "value": formatted, "context": ctx, "positive": positive}
                    )

        dcf = quant.get("dcf_valuation") or {}
        fv = dcf.get("fair_value") or dcf.get("intrinsic_value")
        if fv is not None:
            data.valuation_table.append(("DCF Fair Value", _fmt_dollar(fv)))
            data.scenarios["dcf"] = _fmt_dollar(fv)

        mc = quant.get("monte_carlo") or {}
        if mc.get("p90") is not None:
            data.valuation_table.append(("Bull Case (p90)", _fmt_dollar(mc["p90"])))
            data.scenarios["bull"] = _fmt_dollar(mc["p90"])
        if mc.get("p50") is not None:
            data.valuation_table.append(("Base Case (p50)", _fmt_dollar(mc["p50"])))
            data.scenarios["base"] = _fmt_dollar(mc["p50"])
        if mc.get("p10") is not None:
            data.valuation_table.append(("Bear Case (p10)", _fmt_dollar(mc["p10"])))
            data.scenarios["bear"] = _fmt_dollar(mc["p10"])

        _FUND_MAP = {
            "pe_ratio": ("P/E Ratio", False),
            "trailing_pe": ("P/E Ratio", False),
            "forward_pe": ("Forward P/E", False),
            "roe": ("ROE", True),
            "operating_margin": ("Operating Margin", True),
            "revenue_growth": ("Revenue Growth", True),
            "profit_margin": ("Net Margin", True),
            "current_ratio": ("Current Ratio", False),
            "debt_to_equity": ("Debt/Equity", False),
            "dividend_yield": ("Dividend Yield", True),
            "trailing_eps": ("EPS", False),
        }
        for key, (label, is_pct) in _FUND_MAP.items():
            if key in fundamentals and fundamentals[key] is not None:
                val = fundamentals[key]
                data.financials.append(
                    (
                        label,
                        _fmt_pct(val, is_pct) if is_pct else f"{val:.2f}",
                        _FIN_CONTEXT.get(label, ""),
                    )
                )

        technicals = quant.get("technicals") or {}
        if technicals.get("rsi") is not None:
            rsi = technicals["rsi"]
            data.kpi_chips.append(
                {
                    "label": "RSI",
                    "value": f"{rsi:.0f}",
                    "context": "Momentum (14-day)",
                    "positive": 30 <= rsi <= 70,
                }
            )
            signal = (
                "Overbought"
                if rsi > 70
                else "Bullish"
                if rsi >= 50
                else "Neutral"
                if rsi >= 30
                else "Oversold"
            )
            badge = (
                "expensive"
                if rsi > 70
                else "bullish"
                if rsi >= 50
                else "moderate"
                if rsi >= 30
                else "strong"
            )
            data.scorecard.append(("Momentum", signal, badge))
        if technicals.get("macd_signal") is not None:
            macd = technicals["macd_signal"]
            if isinstance(macd, (int, float)):
                macd_label = "Bullish" if macd > 0 else "Bearish"
                macd_badge = "bullish" if macd > 0 else "expensive"
            else:
                macd = str(macd)
                macd_label = macd.capitalize()
                macd_badge = (
                    "bullish"
                    if "bull" in macd.lower()
                    else "expensive"
                    if "bear" in macd.lower()
                    else "moderate"
                )
            data.scorecard.append(("Technical Outlook", macd_label, macd_badge))
        elif technicals.get("trend"):
            trend = str(technicals["trend"])
            trend_badge = (
                "bullish"
                if "bull" in trend.lower()
                else "expensive"
                if "bear" in trend.lower()
                else "moderate"
            )
            data.scorecard.append(("Technical Outlook", trend.capitalize(), trend_badge))

        peers = quant.get("peer_comparison") or {}
        if isinstance(peers, dict) and peers.get("peers"):
            peer_tickers = [p for p in peers["peers"] if p != data.ticker][:2]
            data.peer_names = peer_tickers
            comparison = peers.get("comparison") or {}
            for key, label in [
                ("pe", "P/E Ratio"),
                ("ev_ebitda", "EV/EBITDA"),
                ("rev_growth", "Revenue Growth"),
                ("op_margin", "Operating Margin"),
                ("roe", "ROE"),
            ]:
                row = {"metric": label}
                tv = comparison.get(data.ticker, {}).get(key)
                is_pct_key = "growth" in key or "margin" in key or "roe" in key
                row["col0"] = (
                    f"{tv * 100:.1f}%"
                    if is_pct_key and isinstance(tv, (int, float))
                    else f"{tv:.1f}"
                    if isinstance(tv, (int, float))
                    else "N/A"
                )
                for ci, pt in enumerate(peer_tickers):
                    pv = comparison.get(pt, {}).get(key)
                    row[f"col{ci + 1}"] = (
                        f"{pv * 100:.1f}%"
                        if is_pct_key and isinstance(pv, (int, float))
                        else f"{pv:.1f}"
                        if isinstance(pv, (int, float))
                        else "N/A"
                    )
                data.peers.append(row)

        stress = quant.get("stress_test") or {}
        if stress.get("cvar_95") is not None:
            data.valuation_table.append(("CVaR (95%)", _fmt_pct(stress["cvar_95"], True)))
        if stress.get("max_drawdown") is not None:
            data.valuation_table.append(("Max Drawdown", _fmt_pct(stress["max_drawdown"], True)))

        if quant.get("recommendation"):
            qrec = str(quant["recommendation"]).upper()
            q_badge = "strong" if qrec == "BUY" else "bullish" if qrec == "HOLD" else "expensive"
            data.scorecard.append(("Quant Signal", qrec, q_badge))

    # ── RAG response ───────────────────────────────────────────────────────
    if rag:
        summary = rag.get("summary") or ""
        if summary:
            data.executive_summary = _strip_markdown(summary[:4000])
        sources = rag.get("sources") or []
        if sources:
            source_text = "\n".join(
                f"- {s}" if isinstance(s, str) else f"- {s.get('title', s.get('url', ''))}"
                for s in sources[:5]
            )
            data.sections.append(Section("Cited Sources", source_text))

    # ── Sentiment response ─────────────────────────────────────────────────
    if sentiment:
        tailwinds = sentiment.get("key_tailwinds") or []
        if tailwinds and not data.opportunities:
            data.opportunities = [_clean_item(t) or t for t in tailwinds[:5] if _clean_item(t)]
            if data.opportunities:
                data.opportunities_extracted = True
        headwinds = sentiment.get("key_headwinds") or []
        if headwinds and not data.risks:
            data.risks = [_clean_item(h) or h for h in headwinds[:5] if _clean_item(h)]
            if data.risks:
                data.risks_extracted = True

        si_peers = sentiment.get("peer_comparison") or []
        if isinstance(si_peers, list) and si_peers and not data.peer_names:
            for pc in si_peers[:2]:
                sym = pc.get("ticker", "")
                if sym and sym != data.ticker and sym not in data.peer_names:
                    data.peer_names.append(sym)
            all_keys = set()
            for pc in si_peers[:2]:
                all_keys.update((pc.get("metrics") or {}).keys())
            for mn in sorted(all_keys):
                row = {"metric": mn, "col0": "N/A"}
                for ci, pc in enumerate(si_peers[:2]):
                    row[f"col{ci + 1}"] = str((pc.get("metrics") or {}).get(mn, "N/A"))
                data.peers.append(row)

        narrative = sentiment.get("narrative") or ""
        if isinstance(narrative, list):
            narrative = " ".join(str(n) for n in narrative)
        elif isinstance(narrative, dict):
            narrative = narrative.get("text") or narrative.get("summary") or str(narrative)
        narrative = str(narrative).strip()
        # Strip leading markdown artifacts (e.g. "**\n\n```json") before code-block check
        narrative = re.sub(r"^\*{1,2}\s*", "", narrative).strip()
        if narrative.startswith("```"):
            narrative = re.sub(r"^```\w*\n?", "", narrative)
            narrative = re.sub(r"\n?```\s*\*{0,2}\s*$", "", narrative)
            try:
                parsed = json.loads(narrative)
                if isinstance(parsed, dict):
                    narrative = parsed.get("narrative", narrative)
                    if isinstance(narrative, list):
                        narrative = " ".join(str(n) for n in narrative)
            except (json.JSONDecodeError, TypeError):
                pass
        if narrative:
            data.sections.append(
                Section("Market Narrative", _strip_markdown(str(narrative)[:4000]))
            )

        signal = str(sentiment.get("overall_signal") or "")
        if signal:
            sig_badge = (
                "bullish"
                if "bull" in signal.lower() or signal.upper() == "BUY"
                else "expensive"
                if "bear" in signal.lower() or signal.upper() == "SELL"
                else "moderate"
            )
            data.scorecard.append(("Market Sentiment", signal.capitalize(), sig_badge))

    # ── Analytics response (legacy dict path) ─────────────────────────────
    if analytics:
        trend = analytics.get("trend_analysis") or {}
        td = trend.get("trend_direction") or "neutral"
        badge = "bullish" if td == "bullish" else "expensive" if td == "bearish" else "moderate"
        data.scorecard.append(("Analytics Trend", td.capitalize(), badge))
        ma_signal = trend.get("ma_crossover_signal")
        if ma_signal:
            ma_badge = "bullish" if ma_signal == "golden_cross" else "expensive"
            data.scorecard.append(("MA Crossover", ma_signal.replace("_", " ").title(), ma_badge))

        forecast = analytics.get("forecast") or {}
        forecast_prices = forecast.get("forecast_prices") or []
        if forecast_prices:
            data.valuation_table.append(("30d Forecast", _fmt_dollar(forecast_prices[-1])))

        anomalies = analytics.get("anomalies") or {}
        if anomalies.get("severity") in ("medium", "high"):
            for fa in anomalies.get("fundamental_anomalies", []):
                data.risks.append(fa)

        stats = analytics.get("statistical_summary") or {}
        if stats.get("skewness") is not None:
            data.financials.append(("Skewness", f"{stats['skewness']:.3f}", "Return Distribution"))
        if stats.get("kurtosis") is not None:
            data.financials.append(("Kurtosis", f"{stats['kurtosis']:.3f}", "Return Distribution"))

        charts = analytics.get("charts") or []
        if charts:
            data.charts = charts

    # ── Reviewer response (legacy dict path) ──────────────────────────────
    if reviewer:
        for c in reviewer.get("contradictions", []):
            if c.get("severity") == "high":
                data.risks.append(f"Contradiction: {c.get('description', '')}")
            data.reviewer_contradictions.append(
                {
                    "agents": ", ".join(c.get("agents", [])),
                    "field": c.get("field", ""),
                    "description": c.get("description", ""),
                    "severity": c.get("severity", ""),
                }
            )

        # Prefer _tool_results (deterministic 0-1 floats) over LLM confidence_breakdown
        tool_results = reviewer.get("_tool_results") or {}
        cb = tool_results.get("confidence") or reviewer.get("confidence_breakdown") or {}
        agent_scores = cb.get("agent_scores") or {}
        if cb.get("meta_confidence") is not None:
            q = _norm_conf(agent_scores.get("quant", 0))
            r = _norm_conf(agent_scores.get("rag", 0))
            mc_c = _norm_conf(agent_scores.get("market_context", 0))
            a = _norm_conf(agent_scores.get("analytics", 0))
            ag = _norm_conf(cb.get("agreement_score", 0))
            dq = _norm_conf(cb.get("data_quality_score", 0))
            meta = _norm_conf(cb["meta_confidence"])
            data.confidence = meta
            data.reviewer_confidence = {
                "quant": f"{q:.0%}",
                "rag": f"{r:.0%}",
                "market_context": f"{mc_c:.0%}",
                "analytics": f"{a:.0%}",
                "agreement": f"{ag:.0%}",
                "data_quality": f"{dq:.0%}",
                "meta": f"{meta:.0%}",
                "quant_raw": q,
                "rag_raw": r,
                "market_context_raw": mc_c,
                "analytics_raw": a,
                "methodology": {
                    "quant": _confidence_reason("quant", q),
                    "rag": _confidence_reason("rag", r),
                    "market_context": _confidence_reason("market_context", mc_c),
                    "analytics": _confidence_reason("analytics", a),
                    "agreement": "Proportion of agents aligned on direction (bullish/bearish/neutral)",
                    "data_quality": f"Non-null fields across all agents ({dq:.0%} populated)",
                    "meta": "Weighted: 40% avg agent confidence + 30% directional agreement + 30% data quality",
                },
            }

        rv = reviewer.get("recommendation_validation") or tool_results.get("validation") or {}
        if rv:
            data.reviewer_validation = {
                "recommendation": rv.get("recommendation", ""),
                "supports": rv.get("evidence_supports", True),
                "strength": (rv.get("evidence_strength") or "moderate").capitalize(),
                "supporting": rv.get("supporting_evidence", [])[:5],
                "contradicting": rv.get("contradicting_evidence", [])[:5],
            }
            if rv.get("evidence_strength") == "strong":
                data.sections.append(
                    Section("Evidence Strength", "Strong evidence supports the recommendation.")
                )

        for sv in reviewer.get("source_verifications", []):
            rate = sv.get("verification_rate", 0)
            data.reviewer_verifications.append(
                {
                    "agent": sv.get("agent_name", ""),
                    "checked": sv.get("claims_checked", 0),
                    "verified": sv.get("claims_verified", 0),
                    "rate": f"{rate:.0%}" if isinstance(rate, float) else str(rate),
                    "unverified": sv.get("unverified_claims", [])[:3],
                }
            )

        for flag in reviewer.get("flags", []):
            data.risks.append(flag)

        review_summary = reviewer.get("review_summary") or ""
        if review_summary:
            data.executive_summary = _truncate_at_sentence(_strip_markdown(review_summary), 4000)

    # ── Executive summary synthesis ────────────────────────────────────────
    if not data.executive_summary:
        parts = []
        if quant.get("reasoning"):
            parts.append(quant["reasoning"][:400])
        if rag.get("summary"):
            parts.append(rag["summary"][:400])
        if sentiment.get("narrative"):
            parts.append(sentiment["narrative"][:400])
        if parts:
            data.executive_summary = _strip_markdown(" ".join(parts)[:4000])

    # ── Final recommendation scorecard entry ───────────────────────────────
    rec = data.recommendation
    data.scorecard.append(
        (
            "Recommendation",
            rec,
            "strong" if rec == "BUY" else "bullish" if rec == "HOLD" else "expensive",
        )
    )


def _populate_from_validated_outputs(data: DeckData, outputs) -> None:
    """Populate DeckData from validated Pydantic agent outputs (ValidatedAgentOutputs).

    Replaces the dict-extraction path in _populate_from_agent_outputs with typed
    attribute access. Falls back gracefully when optional fields are None.
    """
    quant = outputs.quant
    rag = outputs.rag
    sentiment = outputs.market_context
    analytics = outputs.analytics
    reviewer = outputs.reviewer

    # ── Quant ──────────────────────────────────────────────────────────────────
    if quant:
        m = quant.metrics
        # Only add KPI chips when metrics have real (non-zero) values.
        # 0.0 is falsy — this matches existing behaviour: zeroed metrics are skipped.
        if m.sharpe_ratio:
            data.kpi_chips.append(
                {
                    "label": "Sharpe Ratio",
                    "value": f"{m.sharpe_ratio:.2f}",
                    "context": "Risk-adjusted return",
                    "positive": m.sharpe_ratio > 0,
                }
            )
            data.financials.append(("Sharpe Ratio", f"{m.sharpe_ratio:.2f}", "Risk-Adjusted"))
        if m.annual_volatility:
            av = m.annual_volatility
            data.kpi_chips.append(
                {
                    "label": "Annual Volatility",
                    "value": _fmt_pct(av, True),
                    "context": "Annualized",
                    "positive": av < 0.25,
                }
            )
            data.financials.append(("Volatility", _fmt_pct(av, True), "Annualized"))
        if m.beta:
            data.kpi_chips.append(
                {
                    "label": "Beta",
                    "value": f"{m.beta:.2f}",
                    "context": "Mkt sensitivity",
                    "positive": m.beta < 1.0,
                }
            )
            data.financials.append(("Beta", f"{m.beta:.2f}", "vs. S&P 500"))
        if m.var_95_daily:
            data.financials.append(("VaR (95%, daily)", _fmt_pct(m.var_95_daily, True), "Daily"))

        # Fundamentals-based KPI chips when metrics are zeroed (price data unavailable)
        if not data.kpi_chips and quant.fundamentals:
            f = quant.fundamentals
            _CHIP_FUND = [
                ("trailing_pe", "P/E Ratio", False, "Valuation"),
                ("roe", "ROE", True, "Return on Equity"),
                ("operating_margin", "Op. Margin", True, "Profitability"),
                ("revenue_growth", "Rev. Growth", True, "Year-over-year"),
            ]
            for attr, label, is_pct, ctx in _CHIP_FUND:
                val = getattr(f, attr, None)
                if val is not None and len(data.kpi_chips) < 4:
                    fmt = _fmt_pct(val, is_pct) if is_pct else f"{val:.2f}"
                    data.kpi_chips.append(
                        {"label": label, "value": fmt, "context": ctx, "positive": val > 0}
                    )

        # DCF — model uses intrinsic_value (not fair_value)
        if quant.dcf_valuation:
            dcf = quant.dcf_valuation
            data.valuation_table.append(("DCF Fair Value", _fmt_dollar(dcf.intrinsic_value)))
            data.scenarios["dcf"] = _fmt_dollar(dcf.intrinsic_value)

        # Monte Carlo
        if quant.monte_carlo:
            mc = quant.monte_carlo
            if mc.p90 is not None:
                data.valuation_table.append(("Bull Case (p90)", _fmt_dollar(mc.p90)))
                data.scenarios["bull"] = _fmt_dollar(mc.p90)
            if mc.p50 is not None:
                data.valuation_table.append(("Base Case (p50)", _fmt_dollar(mc.p50)))
                data.scenarios["base"] = _fmt_dollar(mc.p50)
            if mc.p10 is not None:
                data.valuation_table.append(("Bear Case (p10)", _fmt_dollar(mc.p10)))
                data.scenarios["bear"] = _fmt_dollar(mc.p10)

        # Fundamentals → financials table
        if quant.fundamentals:
            f = quant.fundamentals
            _FUND_MAP = [
                ("trailing_pe", "P/E Ratio", False),
                ("forward_pe", "Forward P/E", False),
                ("roe", "ROE", True),
                ("operating_margin", "Operating Margin", True),
                ("revenue_growth", "Revenue Growth", True),
                ("profit_margin", "Net Margin", True),
                ("current_ratio", "Current Ratio", False),
                ("debt_to_equity", "Debt/Equity", False),
                ("dividend_yield", "Dividend Yield", True),
            ]
            existing = {row[0] for row in data.financials}
            for attr, label, is_pct in _FUND_MAP:
                val = getattr(f, attr, None)
                if val is not None and label not in existing:
                    data.financials.append(
                        (
                            label,
                            _fmt_pct(val, is_pct) if is_pct else f"{val:.2f}",
                            _FIN_CONTEXT.get(label, ""),
                        )
                    )

        # Technicals → scorecard + KPI + technicals_table
        if quant.technicals:
            t = quant.technicals
            rsi = t.rsi_value
            if rsi is not None:
                data.kpi_chips.append(
                    {
                        "label": "RSI",
                        "value": f"{rsi:.0f}",
                        "context": "Momentum (14-day)",
                        "positive": 30 <= rsi <= 70,
                    }
                )
                signal = (
                    "Overbought"
                    if rsi > 70
                    else "Bullish"
                    if rsi >= 50
                    else "Neutral"
                    if rsi >= 30
                    else "Oversold"
                )
                badge = (
                    "expensive"
                    if rsi > 70
                    else "bullish"
                    if rsi >= 50
                    else "moderate"
                    if rsi >= 30
                    else "strong"
                )
                data.scorecard.append(("Momentum", signal, badge))
                data.technicals_table.append(("RSI (14)", f"{rsi:.1f}", signal))
            if t.macd_signal is not None:
                macd = t.macd_signal
                if isinstance(macd, (int, float)):
                    macd_label = "Bullish" if macd > 0 else "Bearish"
                    macd_badge = "bullish" if macd > 0 else "expensive"
                else:
                    macd_s = str(macd)
                    macd_label = macd_s.capitalize()
                    macd_badge = (
                        "bullish"
                        if "bull" in macd_s.lower()
                        else "expensive"
                        if "bear" in macd_s.lower()
                        else "moderate"
                    )
                data.scorecard.append(("Technical Outlook", macd_label, macd_badge))
            elif t.trend:
                trend_badge = (
                    "bullish"
                    if "bull" in t.trend.lower()
                    else "expensive"
                    if "bear" in t.trend.lower()
                    else "moderate"
                )
                data.scorecard.append(("Technical Outlook", t.trend.capitalize(), trend_badge))

            # Full technicals table
            if t.sma_20 is not None:
                data.technicals_table.append(("SMA 20", f"${t.sma_20:,.2f}", "Short-term trend"))
            if t.sma_50 is not None:
                data.technicals_table.append(("SMA 50", f"${t.sma_50:,.2f}", "Medium-term trend"))
            if t.sma_200 is not None:
                data.technicals_table.append(("SMA 200", f"${t.sma_200:,.2f}", "Long-term trend"))
            if t.ema_12 is not None:
                data.technicals_table.append(("EMA 12", f"${t.ema_12:,.2f}", "Short-term EMA"))
            if t.ema_26 is not None:
                data.technicals_table.append(("EMA 26", f"${t.ema_26:,.2f}", "Long-term EMA"))
            if t.macd is not None:
                data.technicals_table.append(
                    ("MACD", f"{t.macd:.4f}", "Bullish" if t.macd > 0 else "Bearish")
                )
            if t.macd_signal is not None and isinstance(t.macd_signal, (int, float)):
                data.technicals_table.append(("MACD Signal", f"{t.macd_signal:.4f}", ""))
            if t.macd_histogram is not None:
                data.technicals_table.append(
                    (
                        "MACD Histogram",
                        f"{t.macd_histogram:.4f}",
                        "Expanding" if abs(t.macd_histogram) > 0 else "Flat",
                    )
                )
            if t.bb_upper is not None:
                data.technicals_table.append(("Bollinger Upper", f"${t.bb_upper:,.2f}", ""))
            if t.bb_lower is not None:
                data.technicals_table.append(("Bollinger Lower", f"${t.bb_lower:,.2f}", ""))
            if t.bb_position is not None:
                pos_label = (
                    "Near Upper"
                    if t.bb_position > 0.8
                    else "Near Lower"
                    if t.bb_position < 0.2
                    else "Mid-Band"
                )
                data.technicals_table.append(("BB Position", f"{t.bb_position:.2f}", pos_label))
            if t.support is not None:
                data.technicals_table.append(("Support Level", f"${t.support:,.2f}", ""))
            if t.resistance is not None:
                data.technicals_table.append(("Resistance Level", f"${t.resistance:,.2f}", ""))
            if t.momentum_20d is not None:
                data.technicals_table.append(
                    ("Momentum (20d)", _fmt_pct(t.momentum_20d, False), "Short-term")
                )
            if t.momentum_60d is not None:
                data.technicals_table.append(
                    ("Momentum (60d)", _fmt_pct(t.momentum_60d, False), "Medium-term")
                )
            if t.golden_cross is not None:
                data.technicals_table.append(
                    (
                        "Golden Cross",
                        "Yes" if t.golden_cross else "No",
                        "Bullish" if t.golden_cross else "",
                    )
                )
            if t.above_50d_ma is not None:
                data.technicals_table.append(
                    ("Above 50d MA", "Yes" if t.above_50d_ma else "No", "")
                )
            if t.above_200d_ma is not None:
                data.technicals_table.append(
                    ("Above 200d MA", "Yes" if t.above_200d_ma else "No", "")
                )

        # DCF breakdown
        if quant.dcf_valuation:
            dcf = quant.dcf_valuation
            data.dcf_breakdown.append(("Intrinsic Value", _fmt_dollar(dcf.intrinsic_value)))
            data.dcf_breakdown.append(("Current Price", _fmt_dollar(dcf.current_price)))
            data.dcf_breakdown.append(
                (
                    "Upside",
                    _fmt_pct(
                        dcf.upside_pct / 100 if abs(dcf.upside_pct) > 1 else dcf.upside_pct, True
                    ),
                )
            )
            data.dcf_breakdown.append(("WACC", _fmt_pct(dcf.wacc, True)))
            data.dcf_breakdown.append(("Growth Rate", _fmt_pct(dcf.growth_rate, True)))
            data.dcf_breakdown.append(("Terminal Growth", _fmt_pct(dcf.terminal_growth, True)))
            data.dcf_breakdown.append(("Enterprise Value", _fmt_dollar(dcf.enterprise_value)))
            data.dcf_breakdown.append(("FCF Used", _fmt_dollar(dcf.fcf_used)))

        # Monte Carlo summary
        if quant.monte_carlo:
            mc = quant.monte_carlo
            data.monte_carlo_summary = {
                "p10": _fmt_dollar(mc.p10) if mc.p10 else None,
                "p25": _fmt_dollar(mc.p25) if mc.p25 else None,
                "p50": _fmt_dollar(mc.p50) if mc.p50 else None,
                "p75": _fmt_dollar(mc.p75) if mc.p75 else None,
                "p90": _fmt_dollar(mc.p90) if mc.p90 else None,
                "prob_profit": f"{mc.prob_profit:.1%}" if mc.prob_profit else None,
                "expected_return": _fmt_pct(mc.expected_return_pct / 100, True)
                if mc.expected_return_pct
                else None,
                "mc_var_95": _fmt_pct(mc.mc_var_95, True) if mc.mc_var_95 else None,
                "current_price": _fmt_dollar(mc.current_price) if mc.current_price else None,
                "n_simulations": f"{mc.n_simulations:,}" if mc.n_simulations else None,
                "horizon_days": str(mc.horizon_days) if mc.horizon_days else None,
                "p10_raw": mc.p10,
                "p25_raw": mc.p25,
                "p50_raw": mc.p50,
                "p75_raw": mc.p75,
                "p90_raw": mc.p90,
            }

        # Stress test scenarios
        if quant.stress_test and quant.stress_test.scenarios:
            for name, sc in quant.stress_test.scenarios.items():
                decline = _fmt_pct(sc.market_decline_pct, True) if sc.market_decline_pct else "N/A"
                price = _fmt_dollar(sc.projected_price) if sc.projected_price else "N/A"
                data.stress_scenarios.append((name.replace("_", " ").title(), decline, price))

        # Insider signals
        if quant.insider_signals:
            ins = quant.insider_signals
            data.signals_table.append(
                ("Insider", "Activity Level", ins.activity_level.capitalize())
            )
            data.signals_table.append(("Insider", "Direction", ins.direction.capitalize()))
            data.signals_table.append(("Insider", "Buy Signals", str(ins.buy_signals)))
            data.signals_table.append(("Insider", "Sell Signals", str(ins.sell_signals)))
            if ins.net_shares is not None:
                data.signals_table.append(("Insider", "Net Shares", f"{ins.net_shares:,.0f}"))
            if ins.net_value is not None:
                data.signals_table.append(("Insider", "Net Value", _fmt_dollar(ins.net_value)))
            if ins.insider_pct_held is not None:
                data.signals_table.append(
                    ("Insider", "Insider Ownership", _fmt_pct(ins.insider_pct_held, True))
                )

        # Options signals
        if quant.options_signals:
            opt = quant.options_signals
            data.signals_table.append(
                ("Options", "Flow Signal", opt.flow_signal.replace("_", " ").title())
            )
            if opt.put_call_volume_ratio is not None:
                data.signals_table.append(
                    ("Options", "P/C Volume Ratio", f"{opt.put_call_volume_ratio:.2f}")
                )
            if opt.put_call_oi_ratio is not None:
                data.signals_table.append(
                    ("Options", "P/C OI Ratio", f"{opt.put_call_oi_ratio:.2f}")
                )
            if opt.call_volume:
                data.signals_table.append(("Options", "Call Volume", f"{opt.call_volume:,}"))
            if opt.put_volume:
                data.signals_table.append(("Options", "Put Volume", f"{opt.put_volume:,}"))

        # Analyst positioning
        if quant.positioning:
            pos = quant.positioning
            if pos.recommendation_key:
                data.signals_table.append(
                    ("Analyst", "Consensus", pos.recommendation_key.replace("_", " ").title())
                )
            if pos.n_analysts:
                data.signals_table.append(("Analyst", "# Analysts", str(pos.n_analysts)))
            if pos.analyst_target_price is not None:
                data.signals_table.append(
                    ("Analyst", "Target Price", _fmt_dollar(pos.analyst_target_price))
                )
            if pos.analyst_upside_pct is not None:
                data.signals_table.append(
                    (
                        "Analyst",
                        "Target Upside",
                        _fmt_pct(
                            pos.analyst_upside_pct / 100
                            if abs(pos.analyst_upside_pct) > 1
                            else pos.analyst_upside_pct,
                            True,
                        ),
                    )
                )
            if pos.short_ratio is not None:
                data.signals_table.append(("Analyst", "Short Ratio", f"{pos.short_ratio:.1f}"))
            if pos.short_pct_float is not None:
                data.signals_table.append(
                    ("Analyst", "Short % Float", _fmt_pct(pos.short_pct_float, True))
                )
            if pos.short_squeeze_risk:
                data.signals_table.append(("Analyst", "Short Squeeze Risk", "Yes"))

        # Quant signal scores — internal model scores, not shown in report

        # Peer comparison from quant
        if quant.peer_comparison:
            pc = quant.peer_comparison
            peer_tickers = [p for p in pc.peers if p != data.ticker][:2]
            data.peer_names = peer_tickers
            for key, label in [
                ("pe", "P/E Ratio"),
                ("ev_ebitda", "EV/EBITDA"),
                ("rev_growth", "Revenue Growth"),
                ("op_margin", "Operating Margin"),
                ("roe", "ROE"),
            ]:
                row = {"metric": label}
                is_pct_key = "growth" in key or "margin" in key or "roe" in key
                tv = (pc.comparison.get(data.ticker) or {}).get(key)
                row["col0"] = (
                    f"{tv * 100:.1f}%"
                    if is_pct_key and isinstance(tv, (int, float))
                    else f"{tv:.1f}"
                    if isinstance(tv, (int, float))
                    else "N/A"
                )
                for ci, pt in enumerate(peer_tickers):
                    pv = (pc.comparison.get(pt) or {}).get(key)
                    row[f"col{ci + 1}"] = (
                        f"{pv * 100:.1f}%"
                        if is_pct_key and isinstance(pv, (int, float))
                        else f"{pv:.1f}"
                        if isinstance(pv, (int, float))
                        else "N/A"
                    )
                data.peers.append(row)

        # Stress test → valuation table
        if quant.stress_test:
            st = quant.stress_test
            if st.cvar_95 is not None:
                data.valuation_table.append(("CVaR (95%)", _fmt_pct(st.cvar_95, True)))
            if st.var_95 is not None:
                data.valuation_table.append(("VaR (95%)", _fmt_pct(st.var_95, True)))

        # Quant recommendation scorecard entry
        if quant.recommendation:
            qrec = quant.recommendation.upper()
            q_badge = "strong" if qrec == "BUY" else "bullish" if qrec == "HOLD" else "expensive"
            data.scorecard.append(("Quant Signal", qrec, q_badge))

    # ── RAG ────────────────────────────────────────────────────────────────────
    if rag:
        if rag.summary:
            data.executive_summary = _truncate_at_sentence(_strip_markdown(rag.summary), 4000)
        if rag.sources:
            source_text = "\n".join(
                f"- {s}" if isinstance(s, str) else f"- {s.get('title', s.get('url', ''))}"
                for s in rag.sources[:5]
            )
            data.sections.append(Section("Cited Sources", source_text))

    # ── Sentiment / Market Context ──────────────────────────────────────────────
    if sentiment:
        if sentiment.key_tailwinds and not data.opportunities:
            data.opportunities = [
                _clean_item(t) or t for t in sentiment.key_tailwinds[:5] if _clean_item(t)
            ]
            if data.opportunities:
                data.opportunities_extracted = True
        if sentiment.key_headwinds and not data.risks:
            data.risks = [
                _clean_item(h) or h for h in sentiment.key_headwinds[:5] if _clean_item(h)
            ]
            if data.risks:
                data.risks_extracted = True

        # Peer comparison from sentiment (only when quant didn't populate)
        if not data.peer_names and sentiment.peer_comparison:
            for pc in sentiment.peer_comparison[:2]:
                sym = pc.ticker
                if sym and sym != data.ticker and sym not in data.peer_names:
                    data.peer_names.append(sym)
            all_keys: set[str] = set()
            for pc in sentiment.peer_comparison[:2]:
                all_keys.update(pc.metrics.keys())
            for mn in sorted(all_keys):
                row: dict = {"metric": mn, "col0": "N/A"}
                for ci, pc in enumerate(sentiment.peer_comparison[:2]):
                    row[f"col{ci + 1}"] = pc.metrics.get(mn, "N/A")
                data.peers.append(row)

        # Narrative — already clean text (CrewAI output_pydantic strips code blocks)
        if sentiment.narrative:
            data.sections.append(
                Section(
                    "Market Narrative",
                    _truncate_at_sentence(_strip_markdown(sentiment.narrative), 4000),
                )
            )

        if sentiment.overall_signal:
            sig = sentiment.overall_signal
            sig_badge = (
                "bullish"
                if "bull" in sig.lower() or sig.upper() == "BUY"
                else "expensive"
                if "bear" in sig.lower() or sig.upper() == "SELL"
                else "moderate"
            )
            data.scorecard.append(("Market Sentiment", sig.capitalize(), sig_badge))

        if sentiment.macro_regime:
            data.macro_regime = sentiment.macro_regime

    # ── Analytics (validated path) ──────────────────────────────────────────────
    if analytics:
        trend = analytics.trend_analysis
        if trend:
            td = trend.trend_direction or "neutral"
            badge = "bullish" if td == "bullish" else "expensive" if td == "bearish" else "moderate"
            data.scorecard.append(("Analytics Trend", td.capitalize(), badge))
            if trend.ma_crossover_signal:
                badge = "bullish" if trend.ma_crossover_signal == "golden_cross" else "expensive"
                data.scorecard.append(
                    ("MA Crossover", trend.ma_crossover_signal.replace("_", " ").title(), badge)
                )

            if trend.trend_strength is not None:
                ts = trend.trend_strength
                strength_label = (
                    "Strong"
                    if ts > 0.7
                    else "Moderate"
                    if ts > 0.4
                    else "Weak"
                )
            else:
                strength_label = "N/A"
            data.trend_data = {
                "direction": td.capitalize(),
                "strength": strength_label,
                "crossover": trend.ma_crossover_signal.replace("_", " ").title()
                if trend.ma_crossover_signal
                else "None",
                "momentum": trend.momentum_shift.replace("_", " ").title()
                if trend.momentum_shift
                else "Stable",
                "indicators": trend.supporting_indicators[:6]
                if trend.supporting_indicators
                else [],
            }

        forecast = analytics.forecast
        if forecast and forecast.forecast_prices:
            data.valuation_table.append(("30d Forecast", _fmt_dollar(forecast.forecast_prices[-1])))
            data.forecast_chart = {
                "dates": forecast.forecast_dates,
                "prices": forecast.forecast_prices,
                "lower": forecast.confidence_lower,
                "upper": forecast.confidence_upper,
                "method": forecast.method.replace("_", " ").title(),
                "mape": f"{forecast.mape:.2f}%" if forecast.mape else None,
                "horizon": forecast.horizon_days,
                "ci_low": _fmt_dollar(forecast.confidence_lower[-1])
                if forecast.confidence_lower
                else None,
                "ci_high": _fmt_dollar(forecast.confidence_upper[-1])
                if forecast.confidence_upper
                else None,
            }

        anomalies = analytics.anomalies
        if anomalies:
            if anomalies.severity in ("medium", "high"):
                for fa in anomalies.fundamental_anomalies:
                    data.risks.append(fa)

            for a in anomalies.price_anomalies:
                date_str = a.get("date", "N/A")
                if "T" in str(date_str):
                    date_str = str(date_str).split("T")[0]
                atype = a.get("type", "anomaly")
                ret = a.get("return_pct")
                z = a.get("z_score")
                parts = [date_str]
                if atype == "price_spike":
                    parts.append(f"Price surged {ret:+.1f}%" if ret else "Price spike")
                elif atype == "price_drop":
                    parts.append(f"Price fell {ret:+.1f}%" if ret else "Price drop")
                else:
                    parts.append(atype.replace("_", " ").title())
                if z:
                    parts.append(f"({abs(z):.1f}σ deviation)")
                data.anomaly_alerts.append(
                    {
                        "type": "Price",
                        "description": " ".join(parts),
                        "severity": anomalies.severity,
                    }
                )
            for a in anomalies.volume_anomalies:
                date_str = a.get("date", "N/A")
                if "T" in str(date_str):
                    date_str = str(date_str).split("T")[0]
                atype = a.get("type", "anomaly")
                z = a.get("z_score")
                parts = [date_str]
                if z:
                    parts.append(f"Volume {abs(z):.1f}x std above average")
                elif atype == "high_volume":
                    parts.append("Abnormally high volume")
                elif atype == "low_volume":
                    parts.append("Abnormally low volume")
                else:
                    parts.append(atype.replace("_", " ").title())
                data.anomaly_alerts.append(
                    {
                        "type": "Volume",
                        "description": " ".join(parts),
                        "severity": anomalies.severity,
                    }
                )
            for fa in anomalies.fundamental_anomalies:
                data.anomaly_alerts.append(
                    {"type": "Fundamental", "description": fa, "severity": anomalies.severity}
                )
            if anomalies.catalyst_context:
                for ctx_item in anomalies.catalyst_context[:3]:
                    data.anomaly_alerts.append(
                        {"type": "Catalyst", "description": ctx_item, "severity": "info"}
                    )

        stats = analytics.statistical_summary
        if stats:
            if stats.skewness is not None:
                data.financials.append(("Skewness", f"{stats.skewness:.3f}", "Return Distribution"))
                interp = (
                    "Left-skewed (more downside risk)"
                    if stats.skewness < -0.5
                    else "Right-skewed (positive bias)"
                    if stats.skewness > 0.5
                    else "Near symmetric"
                )
                data.stats_table.append(("Skewness", f"{stats.skewness:.3f}", interp))
            if stats.kurtosis is not None:
                data.financials.append(("Kurtosis", f"{stats.kurtosis:.3f}", "Return Distribution"))
                interp = (
                    "Heavy tails (extreme moves likely)"
                    if stats.kurtosis > 3
                    else "Light tails"
                    if stats.kurtosis < 3
                    else "Normal"
                )
                data.stats_table.append(("Kurtosis", f"{stats.kurtosis:.3f}", interp))
            if stats.jarque_bera_pvalue is not None:
                interp = (
                    "Non-normal distribution"
                    if stats.jarque_bera_pvalue < 0.05
                    else "Approximately normal"
                )
                data.stats_table.append(
                    ("Jarque-Bera p-value", f"{stats.jarque_bera_pvalue:.4f}", interp)
                )
            if stats.return_distribution:
                data.stats_table.append(("Distribution", stats.return_distribution, ""))
            if stats.regression_beta is not None:
                data.stats_table.append(
                    ("Regression Beta (1Y)", f"{stats.regression_beta:.3f}", "Market sensitivity")
                )
            if stats.regression_r_squared is not None:
                interp = (
                    "Strong fit"
                    if stats.regression_r_squared > 0.7
                    else "Moderate fit"
                    if stats.regression_r_squared > 0.3
                    else "Weak fit"
                )
                data.stats_table.append(("R-Squared", f"{stats.regression_r_squared:.3f}", interp))
            if stats.correlations:
                for corr_name, corr_val in list(stats.correlations.items())[:5]:
                    data.stats_table.append(
                        (
                            f"Corr: {corr_name}",
                            f"{corr_val:.3f}",
                            "Strong"
                            if abs(corr_val) > 0.7
                            else "Moderate"
                            if abs(corr_val) > 0.3
                            else "Weak",
                        )
                    )

        if analytics.charts:
            data.charts = [
                c.model_dump() if hasattr(c, "model_dump") else c for c in analytics.charts
            ]

    # ── Reviewer (validated path) ───────────────────────────────────────────────
    if reviewer:
        for c in reviewer.contradictions:
            if c.severity == "high":
                data.risks.append(f"Contradiction: {c.description}")
            data.reviewer_contradictions.append(
                {
                    "agents": ", ".join(c.agents),
                    "field": c.field,
                    "description": c.description,
                    "severity": c.severity,
                }
            )

        cb = reviewer.confidence_breakdown
        if cb and cb.meta_confidence is not None:
            q = _norm_conf(cb.agent_scores.quant)
            r = _norm_conf(cb.agent_scores.rag)
            mc = _norm_conf(cb.agent_scores.market_context)
            a = _norm_conf(cb.agent_scores.analytics)
            ag = _norm_conf(cb.agreement_score)
            dq = _norm_conf(cb.data_quality_score)
            meta = _norm_conf(cb.meta_confidence)
            data.confidence = meta
            data.reviewer_confidence = {
                "quant": f"{q:.0%}",
                "rag": f"{r:.0%}",
                "market_context": f"{mc:.0%}",
                "analytics": f"{a:.0%}",
                "agreement": f"{ag:.0%}",
                "data_quality": f"{dq:.0%}",
                "meta": f"{meta:.0%}",
                "quant_raw": q,
                "rag_raw": r,
                "market_context_raw": mc,
                "analytics_raw": a,
                "methodology": {
                    "quant": _confidence_reason("quant", q),
                    "rag": _confidence_reason("rag", r),
                    "market_context": _confidence_reason("market_context", mc),
                    "analytics": _confidence_reason("analytics", a),
                    "agreement": "Proportion of agents aligned on direction (bullish/bearish/neutral)",
                    "data_quality": f"Non-null fields across all agents ({dq:.0%} populated)",
                    "meta": "Weighted: 40% avg agent confidence + 30% directional agreement + 30% data quality",
                },
            }

        for sv in reviewer.source_verifications:
            data.reviewer_verifications.append(
                {
                    "agent": sv.agent_name,
                    "checked": sv.claims_checked,
                    "verified": sv.claims_verified,
                    "rate": f"{sv.verification_rate:.0%}",
                    "unverified": sv.unverified_claims[:3],
                }
            )

        rv = reviewer.recommendation_validation
        if rv:
            data.reviewer_validation = {
                "recommendation": rv.recommendation,
                "supports": rv.evidence_supports,
                "strength": rv.evidence_strength.capitalize(),
                "supporting": rv.supporting_evidence[:5],
                "contradicting": rv.contradicting_evidence[:5],
            }
            if rv.evidence_strength == "strong":
                data.sections.append(
                    Section("Evidence Strength", "Strong evidence supports the recommendation.")
                )

        for flag in reviewer.flags:
            data.risks.append(flag)

        if reviewer.review_summary:
            data.executive_summary = _truncate_at_sentence(
                _strip_markdown(reviewer.review_summary), 4000
            )

    # ── Executive summary synthesis ────────────────────────────────────────────
    if not data.executive_summary:
        parts = []
        if quant and quant.reasoning:
            parts.append(_truncate_at_sentence(quant.reasoning, 400))
        if rag and rag.summary:
            parts.append(_truncate_at_sentence(rag.summary, 400))
        if sentiment and sentiment.narrative:
            parts.append(_truncate_at_sentence(sentiment.narrative, 400))
        if parts:
            data.executive_summary = _truncate_at_sentence(_strip_markdown(" ".join(parts)), 4000)

    # ── Final recommendation scorecard entry ───────────────────────────────────
    rec = data.recommendation
    data.scorecard.append(
        (
            "Recommendation",
            rec,
            "strong" if rec == "BUY" else "bullish" if rec == "HOLD" else "expensive",
        )
    )


def _extract_deck_data(
    brief_data: dict,
    ticker: str,
    recommendation: str,
    confidence: float,
    analysis_date: str,
    company_info: dict | None = None,
) -> DeckData:
    rec = recommendation.upper() if recommendation else "UNKNOWN"
    response_text = brief_data.get("response_text", "")
    name, sector, exchange = _resolve_ticker_info(ticker, response_text, company_info=company_info)

    data = DeckData(
        ticker=ticker,
        company_name=name,
        sector=sector,
        exchange=exchange,
        recommendation=rec,
        confidence=confidence,
        analysis_date=analysis_date,
        executive_summary="",
    )

    # ── Structured InvestmentBrief path ──────────────────────────────────────
    if "final_recommendation" in brief_data or "rag_insights" in brief_data:
        rationale = brief_data.get("recommendation_rationale", "")
        data.executive_summary = _strip_markdown(rationale[:4000]) if rationale else ""
        data.disclaimer = brief_data.get("disclaimer", _DEFAULT_DISCLAIMER)

        qm = brief_data.get("quant_metrics", {})
        si = brief_data.get("market_context", {}) or brief_data.get("sentiment_intelligence", {})
        ri = brief_data.get("rag_insights", {})

        # KPI chips
        if ri.get("revenue_growth_yoy") is not None:
            data.kpi_chips.append(
                {
                    "label": "Revenue Growth",
                    "value": _fmt_pct(ri["revenue_growth_yoy"], True),
                    "context": "Year-over-year",
                    "positive": ri["revenue_growth_yoy"] > 0,
                }
            )
        if qm.get("sharpe_ratio") is not None:
            data.kpi_chips.append(
                {
                    "label": "Sharpe Ratio",
                    "value": f"{qm['sharpe_ratio']:.2f}",
                    "context": "Risk-adjusted return",
                    "positive": qm["sharpe_ratio"] > 0,
                }
            )
        if si.get("avg_price_target"):
            data.kpi_chips.append(
                {
                    "label": "Analyst Target",
                    "value": _fmt_dollar(si["avg_price_target"]),
                    "context": si.get("analyst_consensus", ""),
                    "positive": True,
                }
            )
        if qm.get("annual_volatility") is not None:
            data.kpi_chips.append(
                {
                    "label": "Annual Volatility",
                    "value": _fmt_pct(qm["annual_volatility"], True),
                    "context": "Annualized",
                    "positive": qm["annual_volatility"] < 0.25,
                }
            )

        # Financials table
        if ri.get("revenue_growth_yoy") is not None:
            data.financials.append(
                ("Revenue Growth", _fmt_pct(ri["revenue_growth_yoy"], True), "YoY")
            )
        if qm.get("beta") is not None:
            data.financials.append(("Beta", f"{qm['beta']:.2f}", "vs. S&P 500"))
        if qm.get("sharpe_ratio") is not None:
            data.financials.append(("Sharpe Ratio", f"{qm['sharpe_ratio']:.2f}", "Risk-Adjusted"))
        if qm.get("var_95_daily") is not None:
            data.financials.append(
                ("VaR (95%, daily)", _fmt_pct(qm["var_95_daily"], True), "Daily")
            )

        # Valuation
        if qm.get("dcf_intrinsic_value"):
            data.valuation_table.append(("DCF Fair Value", _fmt_dollar(qm["dcf_intrinsic_value"])))
            data.scenarios["dcf"] = _fmt_dollar(qm["dcf_intrinsic_value"])
        if si.get("avg_price_target"):
            data.valuation_table.append(
                ("Analyst Price Target", _fmt_dollar(si["avg_price_target"]))
            )
            data.scenarios["base"] = _fmt_dollar(si["avg_price_target"])

        # Monte Carlo scenarios (future-proofing — currently flows via response_text)
        mc = qm.get("monte_carlo") or brief_data.get("monte_carlo") or {}
        if mc:
            if mc.get("p90") and "bull" not in data.scenarios:
                data.scenarios["bull"] = _fmt_dollar(mc["p90"])
                data.valuation_table.append(("Bull Case (MC p90)", _fmt_dollar(mc["p90"])))
            if mc.get("p50") and "base" not in data.scenarios:
                data.scenarios["base"] = _fmt_dollar(mc["p50"])
                data.valuation_table.append(("Base Case (MC p50)", _fmt_dollar(mc["p50"])))
            if mc.get("p10"):
                data.scenarios["bear"] = _fmt_dollar(mc["p10"])
                data.valuation_table.append(("Bear Case (MC p10)", _fmt_dollar(mc["p10"])))

        # Scorecard
        data.scorecard.append(
            (
                "Risk Profile",
                "Low"
                if (qm.get("annual_volatility") or 0) < 0.2
                else "Moderate"
                if (qm.get("annual_volatility") or 0) < 0.35
                else "High",
                "moderate",
            )
        )
        if si.get("macro_regime"):
            data.scorecard.append(
                (
                    "Macro Regime",
                    si["macro_regime"],
                    "moderate",
                )
            )
        data.scorecard.append(
            (
                "Recommendation",
                rec,
                "strong" if rec == "BUY" else "bullish" if rec == "HOLD" else "expensive",
            )
        )

        # Risks and opportunities
        ri_risks = ri.get("key_risks", [])
        si_headwinds = si.get("key_headwinds", [])
        combined_risks = (ri_risks + si_headwinds)[:5]
        if combined_risks:
            data.risks = combined_risks
            data.risks_extracted = True
        else:
            data.risks = ["Market volatility", "Regulatory changes"]
        tailwinds = si.get("key_tailwinds", [])
        if tailwinds[:5]:
            data.opportunities = tailwinds[:5]
            data.opportunities_extracted = True
        else:
            data.opportunities = ["Strong operational execution"]

        # ── Peer comparison from structured data ──
        # Quant agent path (future-proofing — currently doesn't flow through)
        qm_peers = qm.get("peer_comparison") or brief_data.get("peer_comparison") or {}
        if isinstance(qm_peers, dict) and qm_peers.get("comparison") and qm_peers.get("peers"):
            peer_tickers = [p for p in qm_peers["peers"] if p != data.ticker][:2]
            data.peer_names = peer_tickers
            comparison = qm_peers["comparison"]
            _pct_metrics = {"rev_growth", "op_margin", "roe"}
            metric_labels = [
                ("pe", "P/E Ratio"),
                ("ev_ebitda", "EV/EBITDA"),
                ("rev_growth", "Revenue Growth"),
                ("op_margin", "Operating Margin"),
                ("roe", "ROE"),
                ("debt_to_equity", "D/E Ratio"),
            ]
            ticker_data = comparison.get(data.ticker, {})
            for key, label in metric_labels:
                row = {"metric": label}
                tv = ticker_data.get(key)
                if isinstance(tv, (int, float)):
                    row["col0"] = f"{tv * 100:.1f}%" if key in _pct_metrics else f"{tv:.1f}"
                else:
                    row["col0"] = "N/A"
                for ci, pt in enumerate(peer_tickers):
                    pv = comparison.get(pt, {}).get(key)
                    if isinstance(pv, (int, float)):
                        row[f"col{ci + 1}"] = (
                            f"{pv * 100:.1f}%" if key in _pct_metrics else f"{pv:.1f}"
                        )
                    else:
                        row[f"col{ci + 1}"] = "N/A"
                data.peers.append(row)

        # Sentiment agent path (CrewAI — pre-formatted string metrics)
        if not data.peers and si.get("peer_comparison"):
            si_peers = si["peer_comparison"]
            if isinstance(si_peers, list) and si_peers:
                for pc_entry in si_peers[:2]:
                    sym = pc_entry.get("ticker", "")
                    if sym and sym != data.ticker and sym not in data.peer_names:
                        data.peer_names.append(sym)
                all_metrics_keys = set()
                for pc_entry in si_peers[:2]:
                    all_metrics_keys.update((pc_entry.get("metrics") or {}).keys())
                for metric_name in sorted(all_metrics_keys):
                    row = {"metric": metric_name, "col0": "N/A"}
                    for ci, pc_entry in enumerate(si_peers[:2]):
                        val = (pc_entry.get("metrics") or {}).get(metric_name, "N/A")
                        row[f"col{ci + 1}"] = str(val)
                    data.peers.append(row)

        # Extra sections
        if ri.get("forward_guidance"):
            data.sections.append(Section("Forward Guidance", ri["forward_guidance"]))
        if si.get("narrative"):
            data.sections.append(Section("Market Narrative", si["narrative"]))

        return data

    # ── Raw agent outputs path ─────────────────────────────────────────────
    if any(
        k in brief_data
        for k in (
            "quant_response",
            "rag_response",
            "sentiment_response",
            "analytics_response",
            "reviewer_response",
        )
    ):
        # Try the validated Pydantic path first; fall back to legacy dict extraction
        # if validation fails (e.g., briefs stored before models were introduced).
        _used_validated = False
        try:
            from pydantic import ValidationError
            from shared.agent_models import (
                AnalyticsAgentOutput,
                MarketContextOutput,
                QuantAgentOutput,
                RAGAgentOutput,
                ReviewerAgentOutput,
                ValidatedAgentOutputs,
            )

            outputs = ValidatedAgentOutputs(
                ticker=ticker,
                quant=QuantAgentOutput.model_validate(brief_data["quant_response"])
                if brief_data.get("quant_response")
                else None,
                rag=RAGAgentOutput.model_validate(brief_data["rag_response"])
                if brief_data.get("rag_response")
                else None,
                market_context=MarketContextOutput.model_validate(brief_data["sentiment_response"])
                if brief_data.get("sentiment_response")
                else None,
                analytics=AnalyticsAgentOutput.model_validate(brief_data["analytics_response"])
                if brief_data.get("analytics_response")
                else None,
                reviewer=ReviewerAgentOutput.model_validate(brief_data["reviewer_response"])
                if brief_data.get("reviewer_response")
                else None,
            )
            _populate_from_validated_outputs(data, outputs)
            _used_validated = True
        except Exception as _ve:
            logger.debug("Validated agent output path failed (%s), using legacy extraction", _ve)
            _populate_from_agent_outputs(data, brief_data, response_text)

        if data.kpi_chips and (data.financials or data.executive_summary):
            return data
        if not _used_validated:
            pass  # already ran legacy path above
        else:
            # Validated path ran but produced sparse data — try legacy as supplement
            _populate_from_agent_outputs(data, brief_data, response_text)

    # ── Minimal response_text path ────────────────────────────────────────────
    response_text = brief_data.get("response_text", "")
    if not response_text:
        data.executive_summary = "No analysis content available."
        return data

    # Parse tables FIRST — before _strip_markdown destroys them
    cleaned_text, table_rows, tables = _parse_markdown_tables(response_text)
    sections = _parse_markdown_sections(cleaned_text)

    # Try to extract structured data from the markdown text
    _enrich_from_markdown(data, cleaned_text, sections, table_rows=table_rows, tables=tables)

    if not data.executive_summary and sections:
        first = sections[0]
        data.executive_summary = (first.body or first.title)[:4000]
        data.sections = sections[1:]
    elif not data.executive_summary:
        data.executive_summary = _strip_markdown(cleaned_text[:4000])

    return data


def _enrich_from_markdown(
    data: DeckData,
    text: str,
    sections: list[Section],
    table_rows: list[dict] = None,
    tables: list[ParsedTable] = None,
) -> None:
    """Extract KPIs, scorecard ratings, risks, and opportunities from text.

    Handles multiple formats:
      - "revenue growth (85.2%)"    — value in parens after label
      - "Revenue Growth: +7.3%"    — colon-separated
      - "ROE=24.1%"                — equals-separated
      - "$298.42"                  — dollar amounts
    """
    # ── Use pre-parsed table data first (R.1: table classification) ──────────
    if tables:
        for parsed_table in tables:
            classification = classify_table(parsed_table.headers, data.ticker)
            if classification == "financial":
                for row in parsed_table.rows:
                    metric = row.get("Metric") or row.get("metric") or ""
                    for val_key in ("Current", "Value", "current", "value"):
                        if val_key in row and metric:
                            data.financials.append(
                                (
                                    metric,
                                    row[val_key],
                                    row.get("YoY Change", row.get("Context", "")),
                                )
                            )
                            break
            elif classification == "peer":
                valid_peers = [
                    h
                    for h in parsed_table.headers
                    if h.lower() != "metric" and h.upper() != data.ticker and _TICKER_RE.match(h)
                ]
                if not valid_peers:
                    # Degradation rule: zero valid peer names → treat as generic
                    continue
                for pn in valid_peers[:2]:
                    if pn not in data.peer_names:
                        data.peer_names.append(pn)
                for row in parsed_table.rows:
                    metric = row.get("Metric") or row.get("metric") or ""
                    if not metric:
                        continue
                    peer_row = {"metric": metric}
                    ticker_val = row.get(data.ticker, "")
                    if ticker_val:
                        peer_row["col0"] = ticker_val
                    for ci, pn in enumerate(valid_peers[:2]):
                        if pn in row:
                            peer_row[f"col{ci + 1}"] = row[pn]
                    if len(peer_row) > 1:
                        data.peers.append(peer_row)
    elif table_rows:
        # Back-compat path (flat list, no classification — removed in R.4)
        for row in table_rows:
            metric = row.get("Metric") or row.get("metric") or ""
            for val_key in ("Current", "Value", "current", "value"):
                if val_key in row and metric:
                    data.financials.append(
                        (metric, row[val_key], row.get("YoY Change", row.get("Context", "")))
                    )
                    break
            peer_cols = [
                k
                for k in row.keys()
                if k not in ("Metric", "metric", "") and k.upper() != data.ticker
            ]
            if peer_cols and metric:
                for peer_name in peer_cols[:2]:
                    if peer_name not in data.peer_names:
                        data.peer_names.append(peer_name)
                peer_row = {"metric": metric}
                ticker_val = row.get(data.ticker, row.get("Current", row.get("current", "")))
                if ticker_val:
                    peer_row["col0"] = ticker_val
                for ci, pn in enumerate(peer_cols[:2]):
                    if pn in row:
                        peer_row[f"col{ci + 1}"] = row[pn]
                if len(peer_row) > 1:
                    data.peers.append(peer_row)

    # ── Extract numeric metrics ──────────────────────────────────────────────
    # Each pattern list is tried in order; first match wins for that metric.
    metric_patterns: list[tuple[str, list[str], bool]] = [
        (
            "Revenue Growth",
            [
                r"revenue\s+growth\s*[\(:]\s*([+-]?\d+\.?\d*)\s*%",
                r"revenue\s+growth[:\s]+([+-]?\d+\.?\d*)\s*%",
                r"revenue\s+growth\s+(?:of\s+)?([+-]?\d+\.?\d*)\s*%",
            ],
            True,
        ),
        (
            "ROE",
            [
                r"ROE\s*[\(:]\s*([+-]?\d+\.?\d*)\s*%",
                r"(?:ROE|return\s+on\s+equity)[:\s]+([+-]?\d+\.?\d*)\s*%",
                r"ROE\s+of\s+([+-]?\d+\.?\d*)\s*%",
            ],
            True,
        ),
        (
            "Operating Margin",
            [
                r"operating\s+margin\s*[\(:]\s*([+-]?\d+\.?\d*)\s*%",
                r"operating\s+margin[:\s]+([+-]?\d+\.?\d*)\s*%",
                r"operating\s+margin\s+(?:stands?\s+at|of|at)\s+([+-]?\d+\.?\d*)\s*%",
            ],
            True,
        ),
        (
            "P/E Ratio",
            [
                r"P/?E\s*[\(:=]\s*([+-]?\d+\.?\d*)",
                r"P/?E\s+(?:ratio\s*)?[:\s]+([+-]?\d+\.?\d*)",
                r"trailing\s+PE\s*[\(:]\s*([+-]?\d+\.?\d*)",
                r"(?:trailing\s+)?P/?E\s+of\s+([+-]?\d+\.?\d*)",
            ],
            False,
        ),
        (
            "Beta",
            [
                r"[Bb]eta\s*[\(:=]\s*([+-]?\d+\.?\d*)",
                r"[Bb]eta[:\s]+([+-]?\d+\.?\d*)",
                r"[Bb]eta\s+of\s+([+-]?\d+\.?\d*)",
            ],
            False,
        ),
        (
            "Sharpe Ratio",
            [
                r"[Ss]harpe\s+[Rr]atio\s*[\(:=]\s*([+-]?\d+\.?\d*)",
                r"[Ss]harpe\s+[Rr]atio[:\s]+([+-]?\d+\.?\d*)",
                r"[Ss]harpe\s+[Rr]atio\s+of\s+([+-]?\d+\.?\d*)",
            ],
            False,
        ),
        (
            "RSI",
            [
                r"RSI\s*[\(:=]\s*([+-]?\d+\.?\d*)",
                r"RSI\s+of\s+([+-]?\d+\.?\d*)",
            ],
            False,
        ),
        (
            "Volatility",
            [
                r"(?:annual\s+)?volatility\s*[\(:]\s*([+-]?\d+\.?\d*)\s*%",
                r"(?:annual\s+)?volatility[:\s]+([+-]?\d+\.?\d*)\s*%",
                r"(?:annual\s+)?volatility\s+of\s+([+-]?\d+\.?\d*)\s*%",
            ],
            True,
        ),
        (
            "Debt/Equity",
            [
                r"(?:debt[/\\]equity|D/E)\s*[\(:=]\s*([+-]?\d+\.?\d*)",
                r"(?:debt[/\\]equity|D/E)\s+of\s+([+-]?\d+\.?\d*)",
            ],
            False,
        ),
        (
            "Dividend Yield",
            [
                r"dividend\s+yield\s*[\(:=]\s*([+-]?\d+\.?\d*)\s*%",
                r"dividend\s+yield\s+of\s+([+-]?\d+\.?\d*)\s*%",
            ],
            True,
        ),
        (
            "EPS",
            [
                r"(?:diluted\s+)?EPS\s*[\(:=]\s*\$?\s*([+-]?\d+\.?\d*)",
                r"(?:diluted\s+)?EPS\s+of\s+\$?\s*([+-]?\d+\.?\d*)",
            ],
            False,
        ),
        (
            "Current Ratio",
            [
                r"current\s+ratio\s*[\(:=]\s*([+-]?\d+\.?\d*)",
                r"current\s+ratio\s+of\s+([+-]?\d+\.?\d*)",
            ],
            False,
        ),
        (
            "Net Margin",
            [
                r"net\s+(?:profit\s+)?margin\s*[\(:=]\s*([+-]?\d+\.?\d*)\s*%",
                r"net\s+(?:profit\s+)?margin\s+of\s+([+-]?\d+\.?\d*)\s*%",
            ],
            True,
        ),
    ]

    extracted: dict[str, str] = {}
    for label, pats, is_pct in metric_patterns:
        for pat in pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                val = m.group(1)
                extracted[label] = f"{val}%" if is_pct else val
                break

    # Priority-based KPI chip selection
    _kpi_context = {
        "Revenue Growth": "YoY",
        "ROE": "Return on Equity",
        "Operating Margin": "Profitability",
        "P/E Ratio": "Valuation",
        "Beta": "Mkt sensitivity",
        "RSI": "Momentum",
        "Sharpe Ratio": "Risk-adj return",
        "Volatility": "Annualized",
        "Debt/Equity": "Leverage",
        "Dividend Yield": "Income",
        "EPS": "Earnings per share",
        "Current Ratio": "Liquidity",
        "Net Margin": "Profitability",
    }
    _KPI_PRIORITY = [
        "Revenue Growth",
        "ROE",
        "Operating Margin",
        "P/E Ratio",
        "Sharpe Ratio",
        "Beta",
        "Dividend Yield",
        "RSI",
        "Volatility",
        "EPS",
        "Net Margin",
        "Debt/Equity",
        "Current Ratio",
    ]
    selected = [lbl for lbl in _KPI_PRIORITY if lbl in extracted][:4]
    for lbl in extracted:
        if lbl not in selected and len(selected) < 4:
            selected.append(lbl)
    for label in selected:
        val = extracted[label]
        data.kpi_chips.append(
            {
                "label": label,
                "value": val,
                "context": _kpi_context.get(label, ""),
                "positive": not val.startswith("-"),
            }
        )

    # ── Extract price targets / valuation ────────────────────────────────────
    # avg target / price target: "$298.42" or "target: $298.42"
    target_pats = [
        r"(?:avg\.?\s+)?(?:price\s+)?target\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"(?:target\s+price|price\s+target|median\s+target)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"(?:average\s+)?target\s+price\s+of\s+\$\s*([\d,]+\.?\d*)",
    ]
    for pat in target_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            data.valuation_table.append(("Analyst Price Target", f"${m.group(1)}"))
            data.scenarios["base"] = f"${m.group(1)}"
            break

    # Upside potential: "45.5% upside" or "upside potential (45.5%)"
    upside_pats = [
        r"(\d+\.?\d*)\s*%\s+upside\s+potential",
        r"upside\s+potential\s*[\(:]\s*(\d+\.?\d*)\s*%",
        r"expected\s+(?:upside|return)\s*[\(:]\s*(\d+\.?\d*)\s*%",
        r"expected\s+(?:upside|return)\s*[=:]\s*([+-]?\d+\.?\d*)\s*%",
        r"(?:analyst|consensus)\s+upside\s*[\(:]\s*(\d+\.?\d*)\s*%",
        r"median\s+upside[:\s]+([+-]?\d+\.?\d*)\s*%",
        r"implying\s+(?:a\s+)?(\d+\.?\d*)\s*%\s+upside",
    ]
    for pat in upside_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1)
            prefix = "" if val.startswith(("+", "-")) else "+"
            data.valuation_table.append(("Expected Upside", f"{prefix}{val}%"))
            break

    dcf_pats = [
        r"DCF\s+(?:fair\s+value|intrinsic\s+value)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"(?:fair\s+value|intrinsic\s+value)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"intrinsic\s+value\s+of\s+\$\s*([\d,]+\.?\d*)",
    ]
    for pat in dcf_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            data.valuation_table.append(("DCF Fair Value", f"${m.group(1)}"))
            data.scenarios["dcf"] = f"${m.group(1)}"
            break

    bull_m = re.search(r"bull\s+case[:\s]*\$\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if bull_m:
        data.valuation_table.append(("Bull Case Target", f"${bull_m.group(1)}"))
        data.scenarios["bull"] = f"${bull_m.group(1)}"

    bear_m = re.search(r"bear\s+case[:\s]*\$\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if bear_m:
        data.valuation_table.append(("Bear Case Target", f"${bear_m.group(1)}"))
        data.scenarios["bear"] = f"${bear_m.group(1)}"

    # Monte Carlo percentile extraction from LLM text
    mc_p90_pats = [
        r"p90\s*[=:]\s*\$\s*([\d,]+\.?\d*)",
        r"90th\s+percentile\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"bull(?:ish)?\s+(?:scenario|outcome)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
    ]
    if "bull" not in data.scenarios:
        for pat in mc_p90_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                data.valuation_table.append(("Bull Case (p90)", f"${m.group(1)}"))
                data.scenarios["bull"] = f"${m.group(1)}"
                break

    mc_p50_pats = [
        r"p50\s*[=:]\s*\$\s*([\d,]+\.?\d*)",
        r"50th\s+percentile\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"median\s+(?:outcome|price|target|value)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
    ]
    if "base" not in data.scenarios:
        for pat in mc_p50_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                data.valuation_table.append(("Base Case (p50)", f"${m.group(1)}"))
                data.scenarios["base"] = f"${m.group(1)}"
                break

    mc_p10_pats = [
        r"p10\s*[=:]\s*\$\s*([\d,]+\.?\d*)",
        r"10th\s+percentile\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"bear(?:ish)?\s+(?:scenario|outcome)\s*[:\s]*\$\s*([\d,]+\.?\d*)",
    ]
    if "bear" not in data.scenarios:
        for pat in mc_p10_pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                data.valuation_table.append(("Bear Case (p10)", f"${m.group(1)}"))
                data.scenarios["bear"] = f"${m.group(1)}"
                break

    prob_pats = [
        r"prob(?:ability)?\s+(?:of\s+)?(?:positive\s+)?(?:return|profit)\s*[:\s]+(\d+\.?\d*)\s*%",
        r"prob(?:ability)?\s+(?:of\s+)?(?:positive\s+)?(?:return|profit)\s*[\(]\s*(\d+\.?\d*)\s*%",
    ]
    for pat in prob_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            data.valuation_table.append(("Prob. of Positive Return", f"{m.group(1)}%"))
            break

    # CVaR / tail risk
    cvar_m = re.search(r"CVaR\s*[\(:=]\s*(-?\d+\.?\d*)\s*%", text, re.IGNORECASE)
    if cvar_m:
        data.valuation_table.append(("CVaR (95%)", f"{cvar_m.group(1)}%"))

    # Current price
    price_pats = [
        r"current\s+price\s+of\s+\$\s*([\d,]+\.?\d*)",
        r"current\s+(?:stock\s+)?price\s*[:\s]*\$\s*([\d,]+\.?\d*)",
        r"(?:trading|priced?)\s+at\s+\$\s*([\d,]+\.?\d*)",
    ]
    for pat in price_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            data.valuation_table.insert(0, ("Current Price", f"${m.group(1)}"))
            break

    # ── Build financials table ───────────────────────────────────────────────
    _fin_context = {
        "Revenue Growth": "Year-over-year",
        "ROE": "Return on Equity",
        "Operating Margin": "Profitability",
        "P/E Ratio": "Price / Earnings",
        "Beta": "Market sensitivity",
        "Sharpe Ratio": "Risk-adjusted return",
        "RSI": "Momentum (14-day)",
        "Volatility": "Annualized",
        "Debt/Equity": "Leverage ratio",
        "Dividend Yield": "Annual yield",
        "EPS": "Earnings per share",
        "Current Ratio": "Liquidity",
        "Net Margin": "Net profitability",
    }
    existing_fin_metrics = {row[0] for row in data.financials}
    for label in (
        "Revenue Growth",
        "ROE",
        "Operating Margin",
        "P/E Ratio",
        "Beta",
        "Sharpe Ratio",
        "RSI",
        "Volatility",
        "Debt/Equity",
        "Dividend Yield",
        "EPS",
        "Current Ratio",
        "Net Margin",
    ):
        if label in extracted and label not in existing_fin_metrics:
            data.financials.append((label, extracted[label], _fin_context.get(label, "")))

    # ── Scorecard ────────────────────────────────────────────────────────────
    # Each entry: (dimension, [(pattern, group_index)], value_map)
    # Patterns try both "label adjective" and "adjective label" orders.
    _STRONG_MAP = {
        "strong": "strong",
        "solid": "strong",
        "robust": "strong",
        "moderate": "moderate",
        "weak": "expensive",
        "poor": "expensive",
    }
    _VALUATION_MAP = {
        "expensive": "expensive",
        "rich": "expensive",
        "premium": "expensive",
        "extreme": "expensive",
        "high": "expensive",
        "elevated": "expensive",
        "cheap": "strong",
        "undervalued": "strong",
        "fair": "moderate",
        "overvalued": "expensive",
    }
    _RISK_MAP = {
        "low": "strong",
        "moderate": "moderate",
        "high": "expensive",
        "elevated": "expensive",
        "significant": "expensive",
    }
    _ANALYST_MAP = {
        "strong buy": "strong",
        "buy": "strong",
        "outperform": "strong",
        "overweight": "strong",
        "hold": "bullish",
        "neutral": "moderate",
        "sell": "expensive",
        "underweight": "expensive",
    }

    scorecard_entries: list[tuple[str, list[str], dict[str, str]]] = [
        (
            "Fundamentals",
            [
                r"(strong|weak|moderate|solid|robust)\s+(?:revenue\s+growth|fundamentals?|earnings)",
                r"fundamental[s]?\s*[:\s]?\s*(strong|weak|moderate|solid|robust)",
            ],
            _STRONG_MAP,
        ),
        (
            "Technical Outlook",
            [
                r"(?:MACD|macd)\s+(?:indicator\s+)?(?:is\s+)?(bullish|bearish)",
                r"(sideways)\s+trend",
                r"(?:technical[s]?|trend)\s*[:\s]?\s*(bullish|bearish|neutral|mixed)",
                r"(bullish|bearish|neutral|mixed)\s+(?:signals?|technicals?|momentum)",
                r"(?<!lack of a )(?<!lack of )(strong\s+uptrend|uptrend|downtrend|golden\s+cross)\b",  # noqa: E501
            ],
            {
                "bullish": "bullish",
                "strong uptrend": "bullish",
                "uptrend": "bullish",
                "golden cross": "bullish",
                "bearish": "expensive",
                "downtrend": "expensive",
                "neutral": "moderate",
                "mixed": "moderate",
                "sideways": "moderate",
            },
        ),
        (
            "Valuation",
            [
                r"(extreme|high|elevated|premium|rich|expensive)\s+valuation",
                r"valuation\s*[:\s]?\s*(expensive|cheap|fair|rich|premium|undervalued|overvalued|extreme|high|elevated)",
                r"(?:stock|company|it)\s+(?:may\s+be|is|appears?)\s+(overvalued|undervalued)",
                r"(?:may\s+be|is)\s+(overvalued|undervalued)\s+relative",
            ],
            _VALUATION_MAP,
        ),
        (
            "Risk Profile",
            [
                r"(significant|elevated|high)\s+(?:tail\s+)?risk",
                r"(?:risk|volatility|tail\s+risk)\s*[:\s]?\s*(low|moderate|high|elevated|significant)",
            ],
            _RISK_MAP,
        ),
        (
            "Profitability",
            [
                r"(robust|strong|high)\s+operating\s+margin",
                r"operating\s+margin\s*[\(:]\s*\d+",
                r"(?:profitability|margins?)\s*[:\s]?\s*(strong|weak|moderate|robust|high|improving)",
            ],
            {
                "strong": "strong",
                "robust": "strong",
                "high": "strong",
                "improving": "bullish",
                "moderate": "moderate",
                "weak": "expensive",
            },
        ),
        (
            "Momentum",
            [
                r"RSI\s*[\(:=]\s*(\d+)",
                r"RSI\s+of\s+(\d+\.?\d*)",
            ],
            {"overbought": "expensive", "oversold": "strong"},
        ),
        (
            "Analyst Sentiment",
            [
                r"""consensus\s+[\"']?(strong\s+buy|buy|hold|sell|outperform|overweight)[\"']?\s+recommend""",
                r"(?:analyst[s]?\s+(?:consensus|recommend|sentiment)|consensus)\s*[:\s]?\s*(strong\s+buy|buy|hold|sell|outperform|overweight|underweight|neutral)",
                r"""recommend[s]?\s+[\"']?(strong\s+buy|buy|hold|sell|outperform|overweight)[\"']?""",
            ],
            _ANALYST_MAP,
        ),
    ]

    for dim, pats, mapping in scorecard_entries:
        for pat in pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                word = m.group(1).lower() if m.lastindex else ""
                badge = mapping.get(word, "moderate")
                # For "operating margin (65.6%)" — just mark as strong if margin > 20%
                if dim == "Profitability" and m.group(0) and "margin" in m.group(0).lower():
                    margin_m = re.search(
                        r"operating\s+margin\s*[\(:]\s*(\d+\.?\d*)", text, re.IGNORECASE
                    )
                    if margin_m and float(margin_m.group(1)) > 20:
                        word, badge = "strong", "strong"
                    elif margin_m and float(margin_m.group(1)) > 10:
                        word, badge = "moderate", "moderate"
                elif dim == "Momentum":
                    rsi_val = float(m.group(1))
                    if rsi_val > 70:
                        word, badge = "Overbought", "expensive"
                    elif rsi_val >= 50:
                        word, badge = "Bullish", "bullish"
                    elif rsi_val >= 30:
                        word, badge = "Neutral", "moderate"
                    else:
                        word, badge = "Oversold", "strong"
                data.scorecard.append((dim, word.capitalize(), badge))
                break
    data.scorecard.append(
        (
            "Recommendation",
            data.recommendation,
            "strong"
            if data.recommendation == "BUY"
            else "bullish"
            if data.recommendation == "HOLD"
            else "expensive",
        )
    )

    # ── Risks and opportunities (all items pass through _clean_item) ─────────
    # From parsed sections
    for sec in sections:
        lower_title = sec.title.lower()
        if any(
            k in lower_title
            for k in ("risk", "threat", "concern", "challenge", "bearish", "headwind")
        ):
            data.risks.extend(
                filter(None, (_clean_item(b) for b in _extract_bullets(sec.body)[:5]))
            )
        elif any(
            k in lower_title
            for k in (
                "opportunit",
                "catalyst",
                "growth",
                "strength",
                "upside",
                "bullish",
                "tailwind",
            )
        ):
            data.opportunities.extend(
                filter(None, (_clean_item(b) for b in _extract_bullets(sec.body)[:5]))
            )

    # From labelled blocks in text: "Key Risks to Monitor:\n" with per-line items
    if not data.risks:
        risk_block = re.search(
            r"(?:key\s+risks?\s*(?:to\s+monitor)?|risk\s+factors?)\s*:\s*\n((?:.*\n)*?)(?:\n\n|\nnext\s+step|\Z)",
            text,
            re.IGNORECASE,
        )
        if risk_block:
            data.risks = list(
                filter(None, (_clean_item(b) for b in _extract_bullets(risk_block.group(1))[:5]))
            )

    # Inline comma-separated risks: "Key Risks: X, Y, and Z."
    if not data.risks:
        inline_risk = re.search(
            r"key\s+risks?\s*:\s*(.+?)(?:\.\s*$|\n\n|\n[A-Z])",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if inline_risk:
            items = re.split(r",\s*(?:and\s+)?", inline_risk.group(1))
            data.risks = list(
                filter(
                    None, (_clean_item(i.strip().rstrip(".")) for i in items if len(i.strip()) > 5)
                )
            )[:5]

    # Bull Case / Bear Case — bounded block + per-item cleaning (R.2)
    if not data.opportunities:
        data.opportunities = _case_items(text, "bull")
    if not data.risks:
        data.risks = _case_items(text, "bear")

    # Growth opportunities / catalysts block with bullets
    if not data.opportunities:
        opp_block = re.search(
            r"(?:growth\s+(?:opportunities|drivers)|catalysts?)\s*:\s*\n((?:[\s\S]*?)(?=\n\n|\n[A-Z][a-z]|\Z))",
            text,
            re.IGNORECASE,
        )
        if opp_block:
            data.opportunities = list(
                filter(None, (_clean_item(b) for b in _extract_bullets(opp_block.group(1))[:5]))
            )

    # Inline bold-labeled blocks: "**Bearish Signals:**\n  - items" or "**Headwinds:** inline text"
    def _extract_labeled_block(label_pat: str) -> list[str]:
        block_m = re.search(
            rf"\*\*{label_pat}\s*:\*\*\s*\n((?:\s+[-•*+].*\n?)+)",
            text,
            re.IGNORECASE,
        )
        if block_m:
            return list(
                filter(None, (_clean_item(b) for b in _extract_bullets(block_m.group(1))[:5]))
            )
        inline_m = re.search(rf"\*\*{label_pat}\s*:\*\*\s*(.+)", text, re.IGNORECASE)
        if inline_m:
            items = re.split(r",\s*(?:and\s+)?", inline_m.group(1))
            return list(
                filter(
                    None, (_clean_item(i.strip().rstrip(".")) for i in items if len(i.strip()) > 5)
                )
            )[:5]
        return []

    if not data.risks:
        for lbl in (r"bearish\s+signals?", r"headwinds?"):
            data.risks.extend(_extract_labeled_block(lbl))
    if not data.opportunities:
        for lbl in (r"bullish\s+signals?", r"tailwinds?"):
            data.opportunities.extend(_extract_labeled_block(lbl))

    if not data.risks:
        data.risks = ["Market volatility", "Regulatory changes"]
    else:
        data.risks_extracted = True
    if not data.opportunities:
        data.opportunities = ["Strong operational execution", "Market positioning"]
    else:
        data.opportunities_extracted = True

    # ── Extract peer tickers from narrative text ─────────────────────────────
    _FINANCIAL_ABBREVS = {
        "DCF",
        "MACD",
        "VIX",
        "DXY",
        "SEC",
        "LLM",
        "WACC",
        "RSI",
        "EPS",
        "EBITDA",
        "IPO",
        "CEO",
        "CFO",
        "ETF",
        "GDP",
        "CPI",
        "FOMC",
        "YOY",
        "QOQ",
        "MOM",
        "ADK",
        "SSE",
        "API",
        "YTD",
    }
    if not data.peer_names:
        # "CompanyName (TICKER)" format
        peer_pattern = re.compile(r"([A-Z][A-Za-z\s]+?)\s*\(([A-Z]{2,5})\)")
        for m in peer_pattern.finditer(text):
            sym = m.group(2)
            if sym != data.ticker and sym not in data.peer_names and sym not in _FINANCIAL_ABBREVS:
                data.peer_names.append(sym)
                if len(data.peer_names) >= 2:
                    break
    if not data.peer_names:
        # "TICKER (CompanyName)" format — e.g., "ORCL (Oracle)"
        reverse_pattern = re.compile(r"\b([A-Z]{2,5})\s*\(([A-Z][A-Za-z\s&.]+?)\)")
        for m in reverse_pattern.finditer(text):
            sym = m.group(1)
            if sym != data.ticker and sym not in data.peer_names and sym not in _FINANCIAL_ABBREVS:
                data.peer_names.append(sym)
                if len(data.peer_names) >= 2:
                    break

    if not data.peer_names:
        # Bare tickers in peer/comparison context: "peers like DLTR", "from COST", "vs. AAPL"
        _bare_peer_pats = [
            r"(?:peers?\s+(?:like|such\s+as|including)|compared?\s+to)\s+([A-Z]{2,5})",
            r"(?:pressure|competition)\s+from\s+([A-Z]{2,5})",
            r"(?:vs\.?|versus)\s+([A-Z]{2,5})",
            r"for\s+([A-Z]{2,5})\s+vs\.",
        ]
        for pat in _bare_peer_pats:
            for m in re.finditer(pat, text):
                sym = m.group(1)
                if (
                    sym != data.ticker
                    and sym not in data.peer_names
                    and sym not in _FINANCIAL_ABBREVS
                ):
                    data.peer_names.append(sym)
                    if len(data.peer_names) >= 2:
                        break
            if len(data.peer_names) >= 2:
                break

    # ── Executive summary ────────────────────────────────────────────────────
    # Build a concise synthesized summary from extracted data
    summary_parts: list[str] = []

    # Lead with key fundamentals
    fund_metrics = []
    for label in ("Revenue Growth", "ROE", "Operating Margin", "P/E Ratio"):
        if label in extracted:
            suffix = "x" if label == "P/E Ratio" else ""
            display_label = label if label in ("ROE", "P/E Ratio") else label.lower()
            fund_metrics.append(f"{display_label} of {extracted[label]}{suffix}")
    if fund_metrics:
        summary_parts.append(f"{data.company_name} demonstrates {', '.join(fund_metrics)}")

    # Valuation context: DCF + current price
    dcf_val = next(
        (
            v
            for lbl, v in data.valuation_table
            if "dcf" in lbl.lower() or "intrinsic" in lbl.lower()
        ),
        None,
    )
    cur_price = next((v for lbl, v in data.valuation_table if "current price" in lbl.lower()), None)
    if dcf_val and cur_price:
        summary_parts.append(
            f"DCF analysis estimates intrinsic value at {dcf_val} versus the current trading price of {cur_price}"  # noqa: E501
        )
    elif dcf_val:
        summary_parts.append(f"DCF analysis estimates intrinsic value at {dcf_val}")

    # Analyst target + upside
    for vlabel, vval in data.valuation_table:
        if "target" in vlabel.lower():
            upside_str = ""
            for ul, uv in data.valuation_table:
                if "upside" in ul.lower():
                    upside_str = f", implying {uv} upside"
                    break
            summary_parts.append(f"Analyst consensus targets {vval}{upside_str}")
            break

    # Technical / scorecard color
    for dim, rating, _ in data.scorecard:
        if dim == "Technical Outlook":
            summary_parts.append(f"Technical outlook is {rating.lower()}")
            break

    # Top opportunity
    if data.opportunities and data.opportunities[0] not in (
        "Strong operational execution",
        "Market positioning",
    ):
        summary_parts.append(data.opportunities[0].rstrip("."))

    # Key risk
    if data.risks and data.risks[0] != "Market volatility":
        first_risk = re.split(r"(?<!\d)\.(?!\d)", data.risks[0])[0]
        summary_parts.append(f"Key risk: {first_risk}")

    if summary_parts:
        data.executive_summary = ". ".join(summary_parts) + "."
        # If metric summary is too short, augment with narrative from Rationale/Thesis section
        if len(data.executive_summary) < 300:
            for sec in sections:
                lt = sec.title.lower()
                if any(
                    k in lt
                    for k in (
                        "rationale",
                        "thesis",
                        "investment thesis",
                        "summary",
                        "synthesis",
                        "outlook",
                    )
                ):
                    narrative = sec.body.strip()
                    if len(narrative) > 50:
                        data.executive_summary = (
                            data.executive_summary.rstrip(".") + ". " + narrative
                        )
                        break
        data.executive_summary = data.executive_summary[:4000]
    else:
        # Fallback: extract from Bull Case or Synthesis block
        bull = re.search(
            r"(?:bull\s+case|synthesis|investment\s+thesis)\s*:\s*(.+?)(?:\n(?:bear\s+case|confidence|key\s+risk)|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if bull:
            raw = bull.group(1).strip()
            first_sent = re.split(r"(?<=\.)\s+", raw)[0]
            data.executive_summary = _strip_markdown(first_sent[:600])
        else:
            lines = text.strip().split("\n")
            substance = []
            for line in lines:
                line = line.strip()
                if (
                    not line
                    or line.startswith("Here is")
                    or line.startswith("Investment Recommendation")
                    or line.startswith("Confidence Score")
                ):
                    continue
                substance.append(line)
                if len(" ".join(substance)) > 400:
                    break
            data.executive_summary = _strip_markdown(" ".join(substance)[:600])
