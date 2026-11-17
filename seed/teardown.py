"""Delete all seeded records from source-sf (anything with a Seed_Key__c).

Pre-existing records (Seed_Key__c = null) are never touched. Requires --yes.
Usage: python -I seed/teardown.py --yes
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules under `python -I`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))  # shared sfcli
OUT = Path(__file__).resolve().parent / "out"
import sfcli  # noqa: E402

# children first; OpportunityContactRoles go with their Opportunities
ORDER = ["Task", "Opportunity", "Contact", "Account"]


def main():
    if "--yes" not in sys.argv:
        sys.exit("Refusing to delete without --yes")
    OUT.mkdir(exist_ok=True)
    for obj in ORDER:
        ids = [r["Id"] for r in sfcli.query(f"SELECT Id FROM {obj} WHERE Seed_Key__c != null")]
        if not ids:
            print(f"{obj:12} nothing to delete")
            continue
        path = OUT / f"delete_{obj.lower()}.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\r\n")
            w.writerow(["Id"])
            w.writerows([i] for i in ids)
        sfcli.run(["data", "delete", "bulk", "--sobject", obj, "--file", str(path), "--line-ending", "CRLF",
                   "--wait", "10"])
        remaining = sfcli.count(f"{obj} WHERE Seed_Key__c != null")
        print(f"{obj:12} deleted {len(ids) - remaining}/{len(ids)}")
        if remaining:
            sys.exit(f"{obj}: {remaining} seeded records remain; stopping")


if __name__ == "__main__":
    main()
