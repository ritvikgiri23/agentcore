from datetime import UTC, datetime

from app.tools.registry import tool


@tool
def get_current_datetime() -> str:
    """Get the current date and time in UTC as an ISO 8601 timestamp."""
    return datetime.now(UTC).isoformat()
