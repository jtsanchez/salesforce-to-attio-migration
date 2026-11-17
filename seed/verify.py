"""Verify seeded data in source-sf against seed/out/manifest.json. Exits 1 on any mismatch."""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules under `python -I`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))  # shared sfcli
OUT = Path(__file__).resolve().parent / "out"
import metrics  # noqa: E402
import sfcli  # noqa: E402

SEEDED = "WHERE Seed_Key__c != null"


def main():
    m = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    checks = []

    def check(name, expected, actual):
        checks.append((name, expected, actual, expected == actual))

    # totals
    for obj in ("Account", "Contact", "Opportunity", "Task"):
        check(f"total {obj}", m["totals"][obj], sfcli.count(f"{obj} {SEEDED}"))
    check("total OpportunityContactRole", m["totals"]["OpportunityContactRole"],
          sfcli.count("OpportunityContactRole WHERE Opportunity.Seed_Key__c != null"))
    for obj, n in m["pre_existing"].items():
        check(f"pre-existing {obj} untouched", n, sfcli.count(f"{obj} WHERE Seed_Key__c = null"))

    accounts = sfcli.query(f"SELECT Phone, BillingCity FROM Account {SEEDED}")
    contacts = sfcli.query(f"SELECT FirstName, LastName, Email, Phone, AccountId FROM Contact {SEEDED}")
    opps = sfcli.query("SELECT StageName, Weekly_Rent__c, Bond__c, Lease_Start_Date__c, Lease_Start_Text__c, "
                       f"Property_State__c FROM Opportunity {SEEDED}")
    tasks = sfcli.query(f"SELECT WhoId, ActivityDate FROM Task {SEEDED}")

    # phones
    def classify(p):
        if not p:
            return "blank"
        return next((n for n, rx in m["phone_format_regex"].items() if re.match(rx, p)), "unrecognised")

    check("phone formats (accounts + contacts)", m["phone_formats"],
          dict(sorted(Counter(classify(r["Phone"]) for r in accounts + contacts).items())))

    # contacts
    check("contacts with no account", m["contacts_no_account"], sum(1 for c in contacts if not c["AccountId"]))
    check("contacts blank email", m["contacts_blank_email"], sum(1 for c in contacts if not c["Email"]))
    check("contacts blank phone", m["contacts_blank_phone"], sum(1 for c in contacts if not c["Phone"]))
    # Salesforce lowercases Email on save, so compare case-insensitively.
    email_counts = Counter(c["Email"].lower() for c in contacts if c["Email"])
    check("duplicate email pairs (case-insensitive)", m["duplicate_email_pairs"],
          sum(1 for n in email_counts.values() if n == 2))
    rows = [{"first": c["FirstName"] or "", "last": c["LastName"], "email": c["Email"], "phone": c["Phone"],
             "account": c["AccountId"]} for c in contacts]
    for name, expected in m["hard_duplicates"].items():
        check(f"hard dup: {name.replace('_', ' ')}", expected, getattr(metrics, name)(rows))

    # accounts
    check("accounts blank phone", m["accounts_blank_phone"], sum(1 for a in accounts if not a["Phone"]))
    check("accounts blank address", m["accounts_blank_address"], sum(1 for a in accounts if not a["BillingCity"]))

    # opportunities
    check("opps by stage", m["opps_by_stage"], dict(sorted(Counter(o["StageName"] for o in opps).items())))
    check("opps with real lease date", m["opps_lease_date"], sum(1 for o in opps if o["Lease_Start_Date__c"]))
    check("opps with text-only lease date", m["opps_lease_text_only"],
          sum(1 for o in opps if o["Lease_Start_Text__c"] and not o["Lease_Start_Date__c"]))
    check("opps with no lease date", m["opps_lease_neither"],
          sum(1 for o in opps if not o["Lease_Start_Text__c"] and not o["Lease_Start_Date__c"]))
    check("opps with legacy state value", m["opps_state_legacy"],
          sum(1 for o in opps if o["Property_State__c"] in m["legacy_state_values"]))
    check("opps with blank state", m["opps_state_blank"], sum(1 for o in opps if not o["Property_State__c"]))
    check("opps blank rent", m["opps_rent_blank"], sum(1 for o in opps if o["Weekly_Rent__c"] is None))
    check("opps blank bond", m["opps_bond_blank"], sum(1 for o in opps if o["Bond__c"] is None))

    # tasks
    check("tasks with no WhoId", m["tasks_no_who"], sum(1 for t in tasks if not t["WhoId"]))
    check("tasks with no due date", m["tasks_no_due_date"], sum(1 for t in tasks if not t["ActivityDate"]))

    width = max(len(c[0]) for c in checks)
    for name, exp, act, ok in checks:
        line = f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {act}"
        if not ok:
            line += f"   (expected {exp})"
        print(line)
    failed = sum(1 for c in checks if not c[3])
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
