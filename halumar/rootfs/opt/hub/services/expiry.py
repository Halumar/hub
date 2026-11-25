"""Guest expiry timestamps: one parser, one storage format, datetime compares.

Every expiry value goes through one parser and is stored in one canonical
format, and every comparison is between aware datetimes, never between text
(string order is not time order for values with different offsets).

Rules:
- Inputs (generate, start-pairing, extend) must carry a timezone offset
  ("Z" or "+01:00"); naive or unparseable values are rejected (422) by the
  Pydantic validators that call ``normalize_expiry_input``.
- Storage is canonical UTC ISO to the second (only microseconds are
  dropped): ``2026-11-18T09:00:30+00:00``, so a guest expires to the second
  and a value less than a minute ahead stays in the future.
- Reading stored values (``parse_stored_expiry``) treats a naive value as
  UTC.
- Comparisons always use aware datetimes.
"""

from datetime import datetime, timedelta, timezone

# Upper bound of one guest grant: guest_access.max_hours of the profile
# catalog (90 days; extendable). Also keeps timedelta away from absurd
# inputs such as 1e12 hours.
MAX_GUEST_HOURS = 24 * 90
MAX_GUEST_DURATION = timedelta(hours=MAX_GUEST_HOURS)


def _utc_canonical(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expiry must be an ISO 8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def normalize_expiry_input(value: str, *, now: datetime | None = None) -> str:
    """Validate a client-supplied expiry and return canonical UTC ISO.

    Raises ValueError (-> 422 from Pydantic) for unparseable values, values
    without a timezone offset, values in the past and values more than 90
    days ahead (one guest grant).
    """
    try:
        dt = _parse_iso(value)
    except ValueError as exc:
        raise ValueError(
            "expiry must be an ISO 8601 date-time, e.g. 2026-11-18T18:00:00+01:00"
        ) from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            "expiry must include a timezone offset ('Z' or e.g. '+01:00'); "
            "a value without offset is ambiguous"
        )
    now = now or datetime.now(timezone.utc)
    if dt <= now:
        raise ValueError("expiry must be in the future")
    if dt - now > MAX_GUEST_DURATION:
        raise ValueError("expiry must be at most 90 days ahead")
    return _utc_canonical(dt)


def expiry_from_hours(hours: float, *, now: datetime | None = None) -> str:
    """Canonical UTC expiry ``hours`` from now (validated by the schemas)."""
    now = now or datetime.now(timezone.utc)
    return _utc_canonical(now + timedelta(hours=hours))


def parse_stored_expiry(value: str | None) -> datetime | None:
    """Parse a stored expiry to an aware UTC datetime.

    None/empty -> None (no expiry). A naive value is read as UTC.
    Raises ValueError for unparseable values; callers decide how to fail.
    """
    if not value:
        return None
    dt = _parse_iso(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_expired(value: str | None, *, now: datetime | None = None) -> bool:
    """True if a stored expiry is reached. Unparseable counts as expired
    (fail closed: a guest whose expiry cannot be read must not stay forever)."""
    try:
        expiry = parse_stored_expiry(value)
    except (ValueError, TypeError):
        return True
    if expiry is None:
        return False
    return (now or datetime.now(timezone.utc)) >= expiry


def canonical_stored_expiry(value: str | None) -> str | None:
    """Migration helper: canonical UTC form of a stored value.

    Returns None for no expiry; raises ValueError for unparseable values.
    """
    expiry = parse_stored_expiry(value)
    if expiry is None:
        return None
    return _utc_canonical(expiry)
