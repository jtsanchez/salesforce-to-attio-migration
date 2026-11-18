"""Pure value-normalisation rules from migrate/mapping.yaml `transforms`.

No I/O and no network. Each function returns the normalised value plus a short `how`
string that transform.py counts in the report, so every rule is testable on its own.
"""
import re
from datetime import date, datetime, timedelta, timezone

STATE_CODES = ("NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT")
LOCATION_KEYS = ("line_1", "line_2", "line_3", "line_4", "locality", "region", "postcode",
                 "country_code", "latitude", "longitude")
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
EARLIEST_LEASE = date(2015, 1, 1)


# --- phones -------------------------------------------------------------------

def phone_e164(raw):
    """Australian phone in any of the source formats -> ("+61XXXXXXXXX", how).

    how: "blank" | "unchanged" (already E.164) | "normalised" | "unparseable" (value is None).
    """
    if raw is None or not str(raw).strip():
        return None, "blank"
    digits = re.sub(r"[\s()\-.]", "", str(raw))
    if digits.startswith("+61"):
        rest = digits[3:]
    elif digits.startswith("61") and len(digits) == 11:
        rest = digits[2:]
    elif digits.startswith("0"):
        rest = digits[1:]
    else:
        return None, "unparseable"
    if not re.fullmatch(r"4\d{8}|[2378]\d{8}", rest):
        return None, "unparseable"
    value = "+61" + rest
    return value, "unchanged" if value == raw else "normalised"


# --- states -------------------------------------------------------------------

def state_normalise(raw, address_state, aliases):
    """Property_State__c + state parsed from the address -> (code, how).

    how: "code" | "alias" | "from_address" | "conflict" (address wins) | "missing" | "unknown".
    """
    addr = address_state if address_state in STATE_CODES else None
    if raw is None or not str(raw).strip():
        return (addr, "from_address") if addr else (None, "missing")
    key = str(raw).strip().lower()
    code = aliases.get(key)
    if code is None:
        return (addr, "unknown") if addr else (None, "unknown")
    how = "code" if str(raw).strip() == code else "alias"
    if addr and addr != code:
        return addr, "conflict"
    return code, how


# --- lease dates --------------------------------------------------------------

def _valid(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_text_date(text):
    """Legacy text date -> (date | None, format name). Day-first, never month-first."""
    t = str(text).strip()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
        return _valid(int(m[1]), int(m[2]), int(m[3])), "iso"
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", t):
        return _valid(int(m[3]), int(m[2]), int(m[1])), "d/mm/yyyy"
    if m := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2})", t):
        return _valid(2000 + int(m[3]), int(m[2]), int(m[1])), "dd.mm.yy"
    if m := re.fullmatch(r"([A-Za-z]{3})[A-Za-z]*\.? (\d{1,2}),? (\d{4})", t):
        month = MONTHS.get(m[1].lower())
        return (_valid(int(m[3]), month, int(m[2])) if month else None), "mon d yyyy"
    return None, "unrecognised"


def lease_start(date_value, text_value, today):
    """Lease_Start_Date__c / Lease_Start_Text__c -> (ISO date | None, how).

    how: "date_field" | "text:<format>" | "missing" | "unparseable" | "out_of_range".
    """
    latest = today + timedelta(days=365)
    if date_value:
        d = date.fromisoformat(date_value)
        return (d.isoformat(), "date_field") if EARLIEST_LEASE <= d <= latest else (None, "out_of_range")
    if not text_value or not str(text_value).strip():
        return None, "missing"
    d, fmt = parse_text_date(text_value)
    if d is None:
        return None, "unparseable"
    if not EARLIEST_LEASE <= d <= latest:
        return None, "out_of_range"
    return d.isoformat(), f"text:{fmt}"


# --- addresses ----------------------------------------------------------------

def empty_location():
    return {k: None for k in LOCATION_KEYS}


def address_from_deal_name(name, pattern):
    """Deal name -> (Attio location dict | None, how). how: "parsed" | "unparseable"."""
    m = re.match(pattern, name or "")
    if not m:
        return None, "unparseable"
    loc = empty_location()
    loc.update(line_1=m["street"].strip(), locality=m["suburb"].strip(), region=m["state"],
               postcode=m["postcode"], country_code="AU")
    return loc, "parsed"


def location_object(street, city, state_code, postcode, country_code):
    """Salesforce address parts -> Attio location (all 10 keys) or None if there is no address."""
    if not any(v and str(v).strip() for v in (street, city, postcode)):
        return None
    loc = empty_location()
    loc.update(line_1=street or None, locality=city or None, region=state_code or None,
               postcode=postcode or None, country_code=country_code or None)
    return loc


# --- small field rules ---------------------------------------------------------

def domain_from_website(raw):
    """"https://www.Foo.example.com/path" -> "foo.example.com"; blank -> None."""
    if not raw or not str(raw).strip():
        return None
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", str(raw).strip().lower())
    d = d.split("/")[0].split("?")[0]
    return d[4:] if d.startswith("www.") else d


LANDLORD_TYPES = {"individual": "Individual", "family trust": "Family trust", "company": "Company"}


def landlord_type_from_description(raw):
    m = re.fullmatch(r"Landlord \((.+)\)", (raw or "").strip())
    return LANDLORD_TYPES.get(m[1].lower()) if m else None


def person_name(first, last):
    first, last = (first or "").strip(), (last or "").strip()
    return {"first_name": first or None, "last_name": last or None,
            "full_name": " ".join(p for p in (first, last) if p)}


def sf_datetime(raw):
    """Salesforce "2026-11-16T00:55:27.000+0000" -> "2026-11-16T00:55:27Z"."""
    if not raw:
        return None
    dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def task_deadline(activity_date):
    """ActivityDate -> 09:00 at a fixed +10:00 (AEST) as ISO 8601; blank -> None."""
    if not activity_date:
        return None
    return f"{date.fromisoformat(activity_date).isoformat()}T09:00:00+10:00"


def task_content(priority, subject, description, status, limit=2000):
    parts = ("[High] " if priority == "High" else "") + (subject or "")
    if description and description.strip():
        parts += "\n\n" + description.strip()
    if status and status not in ("Not Started", "Completed"):
        parts += f"\n[SF status: {status}]"
    return parts[:limit]


def osa_distance(a, b):
    """Damerau-Levenshtein (optimal string alignment): a swap of adjacent letters counts as one edit."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]
