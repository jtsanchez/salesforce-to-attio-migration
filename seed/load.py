"""Deploy fields, assign the permission set, and load seed data into source-sf.

Run seed/generate.py first. Safe to rerun: parents are upserted on Seed_Key__c,
and link.py only emits Tasks / contact roles that don't exist yet.
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules under `python -I`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))  # shared sfcli
OUT = Path(__file__).resolve().parent / "out"
import link  # noqa: E402
import sfcli  # noqa: E402


def bulk(op, sobject, filename, external_id=None):
    args = ["data", f"{op}", "bulk", "--sobject", sobject, "--file", str(OUT / filename),
            "--line-ending", "CRLF", "--wait", "10"]
    if external_id:
        args += ["--external-id", external_id]
    result = sfcli.run(args)
    if "jobInfo" in result:  # `sf data upsert bulk` shape
        info = result["jobInfo"]
        processed, n_failed = info["numberRecordsProcessed"], info["numberRecordsFailed"]
        failed = result.get("records", {}).get("failedResults", [])
    else:  # `sf data import bulk` shape
        processed, n_failed = result.get("processedRecords"), result.get("failedRecords")
        failed = []
        if processed is None or n_failed is None:
            sys.exit(f"{sobject}: unrecognised bulk result: {result}")
    print(f"{op:7} {sobject:24} processed={processed} failed={n_failed}")
    if n_failed:
        for f in failed[:10]:
            print(f"   {f.get('sf__Error')} :: {f.get('Seed_Key__c') or f.get('ContactId') or ''}")
        sys.exit(f"{sobject}: {n_failed} records failed; stopping "
                 f"(details: sf data bulk results --job-id <id> --target-org source-sf)")


def rest_upsert(sobject, filename, external_id="Seed_Key__c", api=sfcli.API_VERSION):
    """Upsert a CSV via the REST composite API with the duplicate-rule allowSave header.

    Used for Contacts: Bulk API can't bypass duplicate-rule alerts, so deliberate
    duplicates of already-saved contacts would be rejected with DUPLICATES_DETECTED.
    Blank cells are omitted (same as bulk: they don't overwrite existing values).
    """
    with open(OUT / filename, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    records = []
    for row in rows:
        rec = {"attributes": {"type": sobject}}
        for col, val in row.items():
            if val == "":
                continue
            if "." in col:  # relationship by external id, e.g. Account.Seed_Key__c
                rel, field = col.split(".", 1)
                rec[rel] = {field: val}
            else:
                rec[col] = val
        records.append(rec)

    created = updated = 0
    errors = []
    body = OUT / f"_rest_{sobject.lower()}.json"
    for i in range(0, len(records), 200):  # composite limit
        chunk = records[i:i + 200]
        body.write_text(json.dumps({"allOrNone": False, "records": chunk}), encoding="utf-8")
        resp = sfcli.rest("PATCH", f"/services/data/{api}/composite/sobjects/{sobject}/{external_id}", body,
                          headers=["Sforce-Duplicate-Rule-Header: allowSave=true"])
        if not isinstance(resp, list):
            sys.exit(f"{sobject}: unexpected REST response: {resp}")
        for rec, res in zip(chunk, resp):
            if res.get("success"):
                created += bool(res.get("created"))
                updated += not res.get("created")
            else:
                errors.append((rec.get(external_id), res.get("errors")))
    body.unlink(missing_ok=True)
    print(f"upsert  {sobject:24} processed={len(records)} created={created} updated={updated} "
          f"failed={len(errors)}  (REST, duplicates allowed)")
    if errors:
        for key, err in errors[:10]:
            print(f"   {key}: {err}")
        sys.exit(f"{sobject}: {len(errors)} records failed; stopping")


def main():
    sfcli.run(["project", "deploy", "start", "--source-dir", "force-app", "--wait", "10"])
    print("deployed force-app metadata")
    try:
        sfcli.run(["org", "assign", "permset", "--name", "Tenancy_Migration"])
        print("assigned Tenancy_Migration")
    except RuntimeError as e:
        if "Duplicate" not in str(e):  # already assigned on reruns
            raise
        print("Tenancy_Migration already assigned")

    bulk("upsert", "Account", "accounts.csv", "Seed_Key__c")
    rest_upsert("Contact", "contacts.csv")
    bulk("upsert", "Opportunity", "opportunities.csv", "Seed_Key__c")

    n_tasks, n_roles = link.main()
    if n_tasks:
        bulk("import", "Task", "tasks.csv")
    if n_roles:
        bulk("import", "OpportunityContactRole", "contact_roles.csv")
    print("load complete")


if __name__ == "__main__":
    main()
