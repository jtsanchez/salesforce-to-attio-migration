"""Resolve seed keys to Salesforce Ids for Tasks and OpportunityContactRoles.

Reads out/tasks_keys.csv and out/contact_roles_keys.csv, writes out/tasks.csv and
out/contact_roles.csv containing only rows not already in the org (so reruns are safe).
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules under `python -I`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))  # shared sfcli
OUT = Path(__file__).resolve().parent / "out"
import sfcli  # noqa: E402


def id_map(sobject):
    return {r["Seed_Key__c"]: r["Id"] for r in
            sfcli.query(f"SELECT Id, Seed_Key__c FROM {sobject} WHERE Seed_Key__c != null")}


def read(name):
    with open(OUT / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write(name, rows, cols):
    with open(OUT / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main():
    contacts, opps = id_map("Contact"), id_map("Opportunity")

    existing_tasks = {r["Seed_Key__c"] for r in
                      sfcli.query("SELECT Seed_Key__c FROM Task WHERE Seed_Key__c != null")}
    tasks = []
    for t in read("tasks_keys.csv"):
        if t["Seed_Key__c"] in existing_tasks:
            continue
        tasks.append({
            "Seed_Key__c": t["Seed_Key__c"], "Subject": t["Subject"],
            "WhatId": opps[t["WhatKey"]], "WhoId": contacts[t["WhoKey"]] if t["WhoKey"] else "",
            "ActivityDate": t["ActivityDate"], "Status": t["Status"], "Priority": t["Priority"],
            "Description": t["Description"],
        })

    existing_roles = {(r["OpportunityId"], r["ContactId"]) for r in sfcli.query(
        "SELECT OpportunityId, ContactId FROM OpportunityContactRole WHERE Opportunity.Seed_Key__c != null")}
    roles = []
    for r in read("contact_roles_keys.csv"):
        pair = (opps[r["OppKey"]], contacts[r["ContactKey"]])
        if pair in existing_roles:
            continue
        roles.append({"OpportunityId": pair[0], "ContactId": pair[1], "Role": r["Role"], "IsPrimary": r["IsPrimary"]})

    n_t = write("tasks.csv", tasks,
                ["Seed_Key__c", "Subject", "WhatId", "WhoId", "ActivityDate", "Status", "Priority", "Description"])
    n_r = write("contact_roles.csv", roles, ["OpportunityId", "ContactId", "Role", "IsPrimary"])
    print(f"link: {n_t} new tasks, {n_r} new contact roles to insert")
    return n_t, n_r


if __name__ == "__main__":
    main()
