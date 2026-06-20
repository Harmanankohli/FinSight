"""Data staleness tracking utilities. Provides helpers to check statement age and data freshness thresholds."""
from datetime import date, datetime


def statement_age_days(statement_date: date | datetime | str) -> int:
    """Return the number of days between today and the given statement date."""
    if isinstance(statement_date, str):
        statement_date = datetime.strptime(statement_date, "%Y-%m-%d").date()
    elif isinstance(statement_date, datetime):
        statement_date = statement_date.date()
    return (date.today() - statement_date).days


def check_freshness(age_days: int) -> dict:
    """Return freshness status and warning message if data is stale."""
    if age_days > 365:
        return {
            "status": "stale",
            "warning": (
                f"Financial statements are {age_days} days old"
                " — data may be significantly outdated"
            ),
        }
    if age_days > 180:
        return {
            "status": "warning",
            "warning": (
                f"Financial statements are {age_days} days old"
                " — consider verifying with latest filings"
            ),
        }
    return {
        "status": "current",
        "warning": None,
    }


def get_latest_statement_date(financials: dict) -> date | None:
    """Extract the most recent statement date from period-keyed financials dict."""
    dates = []
    for period_key in financials:
        try:
            d = datetime.strptime(period_key[:10], "%Y-%m-%d").date()
            dates.append(d)
        except (ValueError, IndexError):
            continue
    return max(dates) if dates else None


def freshness_report(financials: dict) -> dict:
    """Build a full freshness report from raw financials data."""
    latest = get_latest_statement_date(financials)
    if latest is None:
        return {
            "age_days": None,
            "status": "unknown",
            "warning": "Unable to determine financial statement date",
            "latest_date": None,
        }
    age = statement_age_days(latest)
    result = check_freshness(age)
    result["age_days"] = age
    result["latest_date"] = latest.isoformat()
    return result
