"""Extract raw Salesforce data from source-sf into data/raw/ as JSON.

For each object, every queryable field (standard + custom) is selected, pages are
followed via nextRecordsUrl, and the collected count is checked against totalSize.
Values are written exactly as Salesforce returns them; cleanup belongs to the transform step.

Usage: python -I migrate/extract_sf.py [--batch-size N]
Outputs: data/raw/<name>.json, data/raw/<name>.fields.json, data/raw/manifest.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))  # shared sfcli under `python -I`
import sfcli  # noqa: E402

RAW = sfcli.ROOT / "data" / "raw"

# (sObject, output name, use queryAll). queryAll also returns archived Tasks;
# OpportunityContactRole doesn't support it.
OBJECTS = [
    ("Account", "accounts", True),
    ("Contact", "contacts", True),
    ("Opportunity", "opportunities", True),
    ("Task", "tasks", True),
    ("OpportunityContactRole", "opportunity_contact_roles", False),
]
SKIP_TYPES = {"address", "location"}  # compound fields; their components are selected individually
FIELD_KEYS = ("name", "label", "type", "custom", "length", "precision", "scale", "nillable",
              "externalId", "unique", "referenceTo", "relationshipName", "restrictedPicklist")


class ExtractError(RuntimeError):
    pass


def field_metadata(describe):
    """Queryable fields for SOQL plus a trimmed describe for later mapping."""
    fields = [f for f in describe["fields"] if f["type"] not in SKIP_TYPES]
    meta = []
    for f in describe["fields"]:
        entry = {k: f.get(k) for k in FIELD_KEYS}
        if f.get("picklistValues"):
            entry["picklistValues"] = [{"value": p["value"], "label": p["label"], "active": p["active"]}
                                       for p in f["picklistValues"]]
        entry["selected"] = f["type"] not in SKIP_TYPES
        meta.append(entry)
    return [f["name"] for f in fields], meta


def fetch_all(soql, query_all=True, batch_size=None, rest=None):
    """Run a SOQL query through REST, following nextRecordsUrl. Returns (records, pages)."""
    rest = rest or sfcli.rest
    endpoint = "queryAll" if query_all else "query"
    headers = [f"Sforce-Query-Options: batchSize={batch_size}"] if batch_size else []
    path = f"/services/data/{sfcli.API_VERSION}/{endpoint}?q={quote(soql, safe='')}"
    records, pages, total = [], 0, None
    while path:
        page = rest("GET", path, headers=headers)
        if not isinstance(page, dict) or "records" not in page:
            raise ExtractError(f"unexpected query response: {str(page)[:500]}")
        pages += 1
        total = page["totalSize"] if total is None else total
        for r in page["records"]:
            r.pop("attributes", None)
            records.append(r)
        path = None if page.get("done", True) else page.get("nextRecordsUrl")
        if not page.get("done", True) and not path:
            raise ExtractError("response not done but has no nextRecordsUrl")
    if len(records) != total:
        raise ExtractError(f"collected {len(records)} records but totalSize was {total}")
    return records, pages


def write_json(path, data):
    """Write atomically: a partial run never leaves a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch-size", type=int, default=None,
                        help="records per page (200-2000); default is Salesforce's 2000")
    args = parser.parse_args(argv)
    if args.batch_size is not None and not 200 <= args.batch_size <= 2000:
        parser.error("--batch-size must be between 200 and 2000")

    RAW.mkdir(parents=True, exist_ok=True)
    org = sfcli.run(["org", "display"])
    extracted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    summary = []
    for sobject, name, query_all in OBJECTS:
        names, meta = field_metadata(sfcli.describe(sobject))
        soql = f"SELECT {', '.join(names)} FROM {sobject}"
        if query_all:
            soql += " WHERE IsDeleted = false"
        soql += " ORDER BY Id"
        records, pages = fetch_all(soql, query_all=query_all, batch_size=args.batch_size)
        write_json(RAW / f"{name}.json", records)
        write_json(RAW / f"{name}.fields.json", meta)
        custom = sorted(n for n in names if n.endswith("__c"))
        summary.append({"object": sobject, "file": f"{name}.json", "count": len(records),
                        "field_count": len(names), "custom_fields": custom, "pages": pages,
                        "endpoint": "queryAll" if query_all else "query"})
        print(f"  {sobject:<24} {len(records):>5} records, {pages} page(s)")

    write_json(RAW / "manifest.json", {
        "extracted_at": extracted_at,
        "org_id": org.get("id"),
        "instance_url": org.get("instanceUrl"),
        "target_org_alias": sfcli.TARGET_ORG,
        "api_version": sfcli.API_VERSION,
        "objects": {s["object"]: s for s in summary},
        "total_records": sum(s["count"] for s in summary),
    })

    print(f"\nExtracted from {sfcli.TARGET_ORG} at {extracted_at} -> {RAW}")
    print(f"{'Object':<24} {'Records':>8} {'Fields':>7} {'Custom':>7} {'Pages':>6}")
    for s in summary:
        print(f"{s['object']:<24} {s['count']:>8} {s['field_count']:>7} {len(s['custom_fields']):>7} {s['pages']:>6}")
    print(f"{'TOTAL':<24} {sum(s['count'] for s in summary):>8}")


if __name__ == "__main__":
    try:
        main()
    except (ExtractError, RuntimeError) as e:
        sys.exit(f"extract failed: {e}")
