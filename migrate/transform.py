"""Transform the raw Salesforce extract into Attio-shaped JSON. DRY RUN: offline only.

Reads data/raw/*.json and migrate/mapping.yaml; writes data/transformed/:
  companies.json, people.json, deals.json, tasks.json   one object per Attio record:
      {"salesforce_id", "values": {Attio attribute values},
       "references": {attribute: [{"target_object", "salesforce_id"} | {"default_owner": true}]},
       "_migration": {"source_ids", "flags", "orphan"}}
      `references` are resolved to Attio record ids by the loader; `_migration` is never sent to Attio.
  review.csv   one row per flag (merges included) for a human to check
  report.md    records in/out, normalised, flagged, skipped, orphans, expected-count check

This module makes no network calls and writes to no API: it imports nothing that talks to
Salesforce or Attio, and the test suite blocks sockets and subprocesses while it runs.

Usage: .venv/Scripts/python -I migrate/transform.py [--raw data/raw] [--out data/transformed] [--today YYYY-MM-DD]
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules under `python -I`
import yaml  # noqa: E402  (PyYAML, from requirements.txt)

import dedupe as dd  # noqa: E402
import normalise as nz  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAPPING = Path(__file__).resolve().parent / "mapping.yaml"
RAW_FILES = {"Account": "accounts", "Contact": "contacts", "Opportunity": "opportunities",
             "Task": "tasks", "OpportunityContactRole": "opportunity_contact_roles"}
OPPORTUNITY_PREFIX = "006"


class Run:
    """Collects records, flags and counters for one transform run."""

    def __init__(self):
        self.stats = {k: Counter() for k in ("in", "out", "normalised", "flags", "skipped", "orphans", "folded",
                                             "gaps")}
        self.review = []

    def record(self, obj, sf_id, source_ids=None):
        return {"salesforce_id": sf_id, "values": {}, "references": {},
                "_migration": {"object": obj, "source_ids": source_ids or [sf_id], "flags": [], "orphan": []}}

    def flag(self, rec, reason, detail="", name=""):
        rec["_migration"]["flags"].append({"reason": reason, "detail": detail})
        self.stats["flags"][reason] += 1
        self.review.append({"object": rec["_migration"]["object"], "salesforce_id": rec["salesforce_id"],
                            "name": name, "reason": reason, "detail": detail})

    def orphan(self, rec, reason):
        rec["_migration"]["orphan"].append(reason)
        self.stats["orphans"][f"{rec['_migration']['object']}: {reason}"] += 1

    def norm(self, key):
        self.stats["normalised"][key] += 1


def phone_value(e164):
    return {"original_phone_number": e164, "country_code": "AU"}


def transform(raw, mapping, today):
    run = Run()
    t = mapping["transforms"]
    aliases = {str(k).lower(): v for k, v in t["state_normalise"]["aliases"].items()}
    stage_map = t["deal_stage"]["map"]
    addr_pattern = t["address_from_deal_name"]["regex"]
    value_rule = mapping.get("resolved_decisions", {}).get("deal_value", "annual_rent")
    for obj, rows in raw.items():
        run.stats["in"][obj] = len(rows)

    accounts = {a["Id"]: a for a in raw["Account"]}
    opps = {o["Id"]: o for o in raw["Opportunity"]}

    # --- contacts: dedupe first ---------------------------------------------------------
    people_src, merge_map, merges, ambiguous = dd.dedupe(raw["Contact"], mapping["dedupe"])
    remap = lambda cid: merge_map.get(cid, cid)  # noqa: E731
    run.stats["folded"]["Contact merged into another person"] = len(merge_map)

    # contact roles, remapped onto survivors (needed for people's roles and deal links)
    deal_people = defaultdict(list)
    deal_primary = defaultdict(list)
    tenant_ids = set()
    for r in raw["OpportunityContactRole"]:
        if r["OpportunityId"] not in opps:
            run.stats["skipped"]["OpportunityContactRole: deal not in extract"] += 1
            continue
        pid = remap(r["ContactId"])
        if pid not in {p["contact"]["Id"] for p in people_src}:
            run.stats["skipped"]["OpportunityContactRole: contact not in extract"] += 1
            continue
        if pid not in deal_people[r["OpportunityId"]]:
            deal_people[r["OpportunityId"]].append(pid)
        if r.get("IsPrimary"):
            deal_primary[r["OpportunityId"]].append((r.get("CreatedDate") or "", pid))
        if (r.get("Role") or "").lower() == "tenant":
            tenant_ids.add(pid)
        run.stats["folded"]["OpportunityContactRole folded into deal links"] += 1

    # --- companies ------------------------------------------------------------------------
    companies = []
    for a in raw["Account"]:
        rec = run.record("Account", a["Id"])
        v = rec["values"]
        v["salesforce_id"] = a["Id"]
        v["name"] = a["Name"]
        if a.get("Description"):
            v["description"] = a["Description"]
        lt = nz.landlord_type_from_description(a.get("Description"))
        if lt:
            v["landlord_type"] = lt
            run.norm("Account: landlord_type parsed from Description")
        dom = nz.domain_from_website(a.get("Website"))
        if dom:
            v["domains"] = [dom]
            if dom != a["Website"]:
                run.norm("Account: website cleaned to a domain")
        phone, how = nz.phone_e164(a.get("Phone"))
        if phone:
            v["phone"] = phone_value(phone)
            if how == "normalised":
                run.norm("Account: phone normalised to E.164")
        elif how == "unparseable":
            run.flag(rec, "phone_unparseable", a.get("Phone"), a["Name"])
            run.stats["skipped"]["value: unparseable phone omitted"] += 1
        loc = nz.location_object(a.get("BillingStreet"), a.get("BillingCity"), a.get("BillingStateCode"),
                                 a.get("BillingPostalCode"), a.get("BillingCountryCode"))
        if loc:
            v["primary_location"] = loc
        else:
            run.stats["gaps"]["Account: no billing address"] += 1
        v["sf_created_at"] = nz.sf_datetime(a.get("CreatedDate"))
        companies.append(rec)

    # --- people -----------------------------------------------------------------------------
    people = []
    person_ids = set()
    for p in people_src:
        c = p["contact"]
        m = p["merge"]
        rec = run.record("Contact", c["Id"], [c["Id"]] + (m["loser_ids"] if m else []))
        v = rec["values"]
        full_name = f"{c.get('FirstName') or ''} {c.get('LastName') or ''}".strip()
        v["salesforce_id"] = c["Id"]
        v["name"] = nz.person_name(c.get("FirstName"), c.get("LastName"))
        if p["emails"]:
            v["email_addresses"] = p["emails"]
        else:
            run.stats["gaps"]["Contact: no email"] += 1
        raw_phones = [c.get("Phone")] if not m else [x.get("Phone") for x in raw["Contact"]
                                                       if x["Id"] in rec["_migration"]["source_ids"]]
        phones = []
        for rp in raw_phones:
            ph, how = nz.phone_e164(rp)
            if ph and ph not in phones:
                phones.append(ph)
            if how == "normalised":
                run.norm("Contact: phone normalised to E.164")
            elif how == "unparseable":
                run.flag(rec, "phone_unparseable", rp, full_name)
                run.stats["skipped"]["value: unparseable phone omitted"] += 1
        if phones:
            v["phone_numbers"] = [phone_value(ph) for ph in phones]
        loc = nz.location_object(c.get("MailingStreet"), c.get("MailingCity"), c.get("MailingStateCode"),
                                 c.get("MailingPostalCode"), c.get("MailingCountryCode"))
        if loc:
            v["primary_location"] = loc
        titles = m["titles"] if m else ([c["Title"]] if c.get("Title") else [])
        roles = [r for r in ("Landlord", "Tenant") if r in titles or (r == "Tenant" and c["Id"] in tenant_ids)]
        if roles:
            v["crm_roles"] = roles
        if m:
            v["merged_salesforce_ids"] = ",".join(m["loser_ids"])
        v["sf_created_at"] = nz.sf_datetime(c.get("CreatedDate"))

        account = c.get("AccountId")
        tenant_only = "Tenant" in roles and "Landlord" not in roles
        if account and tenant_only:
            rec["_migration"]["dropped_company_link"] = account
            run.stats["gaps"]["Contact: tenant's landlord-account link dropped"] += 1
        elif account:
            rec["references"]["company"] = [{"target_object": "companies", "salesforce_id": account}]
        if not account:
            run.orphan(rec, "no_account_in_salesforce")

        if m:
            run.flag(rec, "duplicate_merged", f"{'+'.join(m['rules'])}: kept {c['Id']}, merged "
                     f"{','.join(m['loser_ids'])} ({' / '.join(m['names'])})", full_name)
            for typo, good in m["dropped_emails"]:
                run.flag(rec, "typo_email_dropped", f"{typo} (kept {good})", full_name)
            for other in m["other_account_ids"]:
                run.flag(rec, "merged_contacts_on_different_accounts",
                         f"kept account {account}; other account {other}", full_name)
            if len(set(m["names"])) > 1:
                run.norm("Contact: name chosen from merged variants")
        people.append(rec)
        person_ids.add(c["Id"])

    for c, candidates in ambiguous:
        rec = next(r for r in people if c["Id"] in r["_migration"]["source_ids"])
        run.flag(rec, "nickname_ambiguous", "matches " + ", ".join(
            f"{o.get('FirstName')} {o.get('LastName')} ({o['Id']})" for o in candidates),
            f"{c.get('FirstName')} {c.get('LastName')}")

    # --- deals -------------------------------------------------------------------------------
    deals = []
    for o in raw["Opportunity"]:
        rec = run.record("Opportunity", o["Id"])
        v = rec["values"]
        v["salesforce_id"] = o["Id"]
        v["name"] = o["Name"]
        stage = stage_map.get(o.get("StageName"))
        if stage:
            v["stage"] = stage
            run.norm("Opportunity: stage mapped to an Attio stage")
        else:
            run.flag(rec, "stage_unmapped", o.get("StageName"), o["Name"])
        v["sf_stage"] = o.get("StageName")

        rent, bond = o.get("Weekly_Rent__c"), o.get("Bond__c")
        if rent is not None:
            v["weekly_rent"] = rent
            v["value"] = round(rent * 52, 2) if value_rule == "annual_rent" else rent
        else:
            run.stats["gaps"]["Opportunity: no weekly rent"] += 1
        if bond is not None:
            v["bond"] = bond
            if rent and bond > 4 * rent:
                run.flag(rec, "bond_over_4_weeks", f"bond {bond} = {bond / rent:.1f} weeks of {rent}", o["Name"])
        else:
            run.stats["gaps"]["Opportunity: no bond"] += 1

        lease, how = nz.lease_start(o.get("Lease_Start_Date__c"), o.get("Lease_Start_Text__c"), today)
        if lease:
            v["lease_start_date"] = lease
            if how.startswith("text:"):
                run.norm(f"Opportunity: lease start parsed from text ({how[5:]})")
        else:
            run.flag(rec, "lease_start_missing", f"{how}; text={o.get('Lease_Start_Text__c')!r}", o["Name"])
        if o.get("Lease_Start_Text__c"):
            v["lease_start_legacy_text"] = o["Lease_Start_Text__c"]

        addr, how = nz.address_from_deal_name(o["Name"], addr_pattern)
        if addr:
            v["property_address"] = addr
            run.norm("Opportunity: property address parsed from name")
        else:
            run.flag(rec, "address_unparseable", o["Name"], o["Name"])
        state, how = nz.state_normalise(o.get("Property_State__c"), addr["region"] if addr else None, aliases)
        if state:
            v["property_state"] = state
        if how == "alias":
            run.norm("Opportunity: legacy state value mapped to a code")
        elif how == "from_address":
            run.norm("Opportunity: blank state filled from the address")
        elif how == "conflict":
            run.flag(rec, "state_conflict", f"field {o.get('Property_State__c')!r}, address {state}", o["Name"])
        elif how in ("missing", "unknown"):
            run.flag(rec, "state_unknown", repr(o.get("Property_State__c")), o["Name"])

        if o.get("CloseDate"):
            v["sf_close_date"] = o["CloseDate"]
        v["sf_created_at"] = nz.sf_datetime(o.get("CreatedDate"))

        refs = rec["references"]
        refs["owner"] = [{"default_owner": True}]
        refs["associated_company"] = [{"target_object": "companies", "salesforce_id": o["AccountId"]}]
        if deal_people[o["Id"]]:
            refs["associated_people"] = [{"target_object": "people", "salesforce_id": pid}
                                         for pid in deal_people[o["Id"]]]
        primaries = sorted(deal_primary[o["Id"]])
        if primaries:
            refs["primary_tenant"] = [{"target_object": "people", "salesforce_id": primaries[0][1]}]
            if len({pid for _, pid in primaries}) > 1:
                run.flag(rec, "multiple_primary_tenants", ", ".join(pid for _, pid in primaries), o["Name"])
        deals.append(rec)

    # --- tasks -------------------------------------------------------------------------------
    tasks = []
    for tk in raw["Task"]:
        what = tk.get("WhatId")
        if not what or not what.startswith(OPPORTUNITY_PREFIX) or what not in opps:
            run.stats["skipped"]["Task: not linked to a migrated deal"] += 1
            continue
        rec = run.record("Task", tk["Id"])
        v = rec["values"]
        v["content"] = nz.task_content(tk.get("Priority"), tk.get("Subject"), tk.get("Description"),
                                       tk.get("Status"))
        if tk.get("Status") not in ("Not Started", "Completed"):
            run.norm("Task: Salesforce status kept in content")
        v["format"] = "plaintext"
        v["deadline_at"] = nz.task_deadline(tk.get("ActivityDate"))
        if not v["deadline_at"]:
            run.stats["gaps"]["Task: no due date"] += 1
        v["is_completed"] = bool(tk.get("IsClosed"))
        links = [{"target_object": "deals", "salesforce_id": what}]
        if tk.get("WhoId"):
            who = remap(tk["WhoId"])
            if who != tk["WhoId"]:
                run.norm("Task: contact remapped to merged survivor")
            links.append({"target_object": "people", "salesforce_id": who})
        else:
            run.orphan(rec, "no_contact")
        rec["references"]["linked_records"] = links
        rec["references"]["assignees"] = [{"default_owner": True}]
        tasks.append(rec)

    # --- orphans that need the whole picture ----------------------------------------------------
    linked_people = {r["salesforce_id"] for d in deals for r in d["references"].get("associated_people", [])}
    for rec in people:
        if "company" not in rec["references"] and rec["salesforce_id"] not in linked_people:
            run.orphan(rec, "no_company_and_no_deal")
    companies_with_links = ({d["references"]["associated_company"][0]["salesforce_id"] for d in deals}
                            | {r["references"]["company"][0]["salesforce_id"] for r in people
                               if "company" in r["references"]})
    for rec in companies:
        if rec["salesforce_id"] not in companies_with_links:
            run.orphan(rec, "no_deals_and_no_people")

    for name, rows in (("companies", companies), ("people", people), ("deals", deals), ("tasks", tasks)):
        run.stats["out"][name] = len(rows)
    links = {
        "deal_people_links": sum(len(d["references"].get("associated_people", [])) for d in deals),
        "deals_with_primary_tenant": sum(1 for d in deals if "primary_tenant" in d["references"]),
        "task_person_links": sum(1 for tk in tasks if len(tk["references"]["linked_records"]) > 1),
    }
    return {"companies": companies, "people": people, "deals": deals, "tasks": tasks,
            "merges": merges, "review": run.review, "stats": run.stats, "links": links}


# --- I/O --------------------------------------------------------------------------------------

def load_raw(raw_dir):
    raw = {obj: json.loads((raw_dir / f"{name}.json").read_text(encoding="utf-8")) for obj, name in RAW_FILES.items()}
    manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    return raw, manifest


def load_mapping(path=MAPPING):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def expected_check(result, mapping):
    exp = mapping.get("expected_counts", {})
    actual = {**{k: result["stats"]["out"][k] for k in ("companies", "people", "deals", "tasks")}, **result["links"]}
    rows = [(k, exp[k], actual[k]) for k in actual if isinstance(exp.get(k), int)]
    for k, v in (exp.get("flags") or {}).items():
        key = {"phone_unparseable": "phone_unparseable", "address_unparseable": "address_unparseable"}.get(k, k)
        rows.append((f"flags.{k}", v, result["stats"]["flags"][key]))
    return rows


def render_report(result, mapping, manifest, generated_at, raw_dir):
    s = result["stats"]
    lines = [
        "# Transform report (dry run)",
        "",
        f"- Generated: {generated_at}",
        f"- Source extract: `{raw_dir.as_posix()}`, extracted {manifest.get('extracted_at')} from "
        f"`{manifest.get('target_org_alias')}` (org {manifest.get('org_id')})",
        "- Mapping: `migrate/mapping.yaml`",
        "- **Dry run:** nothing was sent to Salesforce or Attio. Output is local JSON only.",
        "",
        "## Records in → out",
        "",
        "| Salesforce object | In | Attio object | Out | Note |",
        "|---|---:|---|---:|---|",
    ]
    notes = {"Account": ("companies", ""), "Contact": ("people", f"{len(result['merges'])} duplicate groups merged"),
             "Opportunity": ("deals", ""), "Task": ("tasks", ""),
             "OpportunityContactRole": ("(deal links)", "folded into deals' associated_people / primary_tenant")}
    for obj in RAW_FILES:
        target, note = notes[obj]
        out = s["out"].get(target, result["links"]["deal_people_links"] if obj == "OpportunityContactRole" else "")
        lines.append(f"| {obj} | {s['in'][obj]} | {target} | {out} | {note} |")

    def table(title, counter, empty, header="Count"):
        lines.extend(["", f"## {title}", ""])
        if not counter:
            lines.append(empty)
            return
        lines.extend([f"| What | {header} |", "|---|---:|"])
        lines.extend(f"| {k} | {v} |" for k, v in sorted(counter.items()))
        lines.append(f"| **Total** | **{sum(counter.values())}** |")

    table("Normalised", s["normalised"], "Nothing needed normalising.")
    table("Flagged for review", s["flags"], "Nothing flagged.", "Flags")
    lines.append("")
    lines.append("Every flag is a row in `review.csv`. `duplicate_merged` is one row per merged group.")
    skipped = Counter(s["skipped"])
    table("Skipped", skipped, "No records or values were skipped.")
    folded = s["folded"]
    lines.extend(["", "Not skipped, but not 1:1 either:", ""])
    lines.extend(f"- {k}: {v}" for k, v in sorted(folded.items()))
    table("Orphans kept (marked in `_migration.orphan`)", s["orphans"], "No orphans.")
    table("Gaps (blank values left empty)", s["gaps"], "No gaps.")

    lines.extend(["", "## Merged duplicate groups", "",
                  "| Survivor | Merged | Rules | Names | Dropped emails |", "|---|---|---|---|---|"])
    for m in result["merges"]:
        dropped = ", ".join(t for t, _ in m["dropped_emails"]) or ""
        lines.append(f"| {m['survivor_id']} | {', '.join(m['loser_ids'])} | {', '.join(m['rules'])} | "
                     f"{' / '.join(m['names'])} | {dropped} |")

    rows = expected_check(result, mapping)
    lines.extend(["", "## Check against `mapping.yaml` expected_counts", "",
                  "| Count | Expected | Actual | |", "|---|---:|---:|---|"])
    lines.extend(f"| {k} | {e} | {a} | {'OK' if e == a else '**MISMATCH**'} |" for k, e, a in rows)
    return "\n".join(lines) + "\n"


def write_outputs(result, out_dir, report):
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("companies", "people", "deals", "tasks"):
        write_json(out_dir / f"{name}.json", result[name])
    tmp = out_dir / "review.csv.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["object", "salesforce_id", "name", "reason", "detail"])
        w.writeheader()
        w.writerows(sorted(result["review"], key=lambda r: (r["reason"], r["object"], r["salesforce_id"])))
    os.replace(tmp, out_dir / "review.csv")
    tmp = out_dir / "report.md.tmp"
    tmp.write_text(report, encoding="utf-8")
    os.replace(tmp, out_dir / "report.md")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dry-run transform: data/raw -> data/transformed (no network).")
    ap.add_argument("--raw", type=Path, default=ROOT / "data" / "raw")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "transformed")
    ap.add_argument("--mapping", type=Path, default=MAPPING)
    ap.add_argument("--today", type=date.fromisoformat, default=date.today(),
                    help="reference date for the lease-date sanity window (default: today)")
    args = ap.parse_args(argv)

    raw, manifest = load_raw(args.raw)
    mapping = load_mapping(args.mapping)
    result = transform(raw, mapping, args.today)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = render_report(result, mapping, manifest, generated_at, args.raw)
    write_outputs(result, args.out, report)

    s = result["stats"]
    mismatches = [r for r in expected_check(result, mapping) if r[1] != r[2]]
    print(f"in:  {dict(s['in'])}")
    print(f"out: {dict(s['out'])}")
    print(f"normalised {sum(s['normalised'].values())}, flagged {sum(s['flags'].values())}, "
          f"skipped {sum(s['skipped'].values())}, orphans {sum(s['orphans'].values())}")
    print(f"wrote {args.out} (report.md, review.csv)")
    if mismatches:
        print("expected_counts MISMATCH: " + "; ".join(f"{k} expected {e} got {a}" for k, e, a in mismatches))
        return 1
    print("expected_counts: all match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
