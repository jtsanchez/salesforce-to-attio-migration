"""Generate deterministic, deliberately messy property-agency demo data as CSVs.

Output (seed/out/):
  accounts.csv, contacts.csv, opportunities.csv  -> upserted on Seed_Key__c
  tasks_keys.csv, contact_roles_keys.csv         -> resolved to Ids by link.py
  manifest.json                                   -> expected counts for verify.py

All people are fictional, all emails use RFC 2606 example domains, and all phone
numbers come from ACMA's ranges reserved for fiction.
"""
import csv
import json
import random
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules under `python -I`
import metrics  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
rng = random.Random(42)

# --- reference data -----------------------------------------------------------

FIRST = [
    "Amelia", "Liam", "Charlotte", "Noah", "Isla", "Jack", "Olivia", "William", "Mia", "Henry",
    "Ava", "Leo", "Grace", "Oliver", "Chloe", "Lucas", "Zara", "Ethan", "Ruby", "Mason",
    "Priya", "Arjun", "Mei", "Wei", "Thi", "Minh", "Aisha", "Omar", "Fatima", "Yusuf",
    "Sienna", "Harrison", "Matilda", "Archie", "Evie", "Hamish", "Tahlia", "Darcy", "Ivy", "Felix",
    "Kiri", "Tane", "Ngaire", "Rangi", "Lachlan", "Bronwyn", "Declan", "Imogen", "Rohan", "Anika",
]
LAST = [
    "Whitfield", "Nguyen", "Kowalczyk", "Papadopoulos", "Fitzgerald", "Tran", "Hargreaves", "Singh",
    "Castellano", "Brennan", "Abernethy", "Chandra", "Delacroix", "Ellery", "Fairbairn", "Galloway",
    "Holloway", "Iskander", "Jarrah", "Kingsley", "Lombardi", "McAlister", "Novak", "Oyelaran",
    "Petrakis", "Quinlan", "Rasmussen", "Sorensen", "Thornbury", "Underwood", "Varga", "Wakefield",
    "Yilmaz", "Zielinski", "Ashcombe", "Blackwood", "Calloway", "Dunmore", "Everingham", "Ferreira",
    "Gallagher", "Haddad", "Ivanova", "Jovanovic", "Kerrigan", "Larkspur", "Mahoney", "Nakamura",
]
COMPANY_WORDS = [
    "Harbourline", "Southerly", "Ironbark", "Bluegum", "Coastal Crest", "Redgum", "Saltbush",
    "Kookaburra Hill", "Wattlebank", "Brightwater", "Tidewater", "Granite Ridge",
]
STREETS = [
    "Banksia St", "Wattle Ave", "Grevillea Cres", "Jacaranda Rd", "Bottlebrush Ln", "Waratah Pde",
    "Melaleuca Way", "Kurrajong St", "Boronia Ct", "Callistemon Dr", "Myrtle Tce", "Lilly Pilly Cl",
    "Paperbark Rd", "Saltwater Pl", "Tallowwood St", "Ironwood Ave",
]
# Real suburbs, (suburb, state code, postcode, phone area code)
SUBURBS = [
    ("Marrickville", "NSW", "2204", "02"), ("Parramatta", "NSW", "2150", "02"), ("Newtown", "NSW", "2042", "02"),
    ("Penrith", "NSW", "2750", "02"), ("Wollongong", "NSW", "2500", "02"), ("Newcastle", "NSW", "2300", "02"),
    ("Brunswick", "VIC", "3056", "03"), ("Footscray", "VIC", "3011", "03"), ("Richmond", "VIC", "3121", "03"),
    ("Geelong", "VIC", "3220", "03"), ("Ballarat Central", "VIC", "3350", "03"),
    ("Fortitude Valley", "QLD", "4006", "07"), ("Southport", "QLD", "4215", "07"), ("Toowong", "QLD", "4066", "07"),
    ("Fremantle", "WA", "6160", "08"), ("Subiaco", "WA", "6008", "08"),
    ("Norwood", "SA", "5067", "08"), ("Glenelg", "SA", "5045", "08"),
    ("Sandy Bay", "TAS", "7005", "03"), ("Braddon", "ACT", "2612", "02"), ("Parap", "NT", "0820", "08"),
]
# Legacy values must differ by more than case: Salesforce folds unrestricted picklist
# values case-insensitively onto defined ones (e.g. "Qld" is stored as "QLD").
LEGACY_STATE = {"VIC": "Victoria", "NSW": "N.S.W.", "QLD": "Queensland", "WA": "W.A.", "SA": "Sth Aust"}

# ACMA fictional-use mobiles (all 30 of them)
MOBILES = [
    "0491570006", "0491570156", "0491570157", "0491570158", "0491570159", "0491570110", "0491570313",
    "0491570737", "0491571266", "0491571491", "0491571804", "0491572549", "0491572665", "0491572983",
    "0491573770", "0491573087", "0491574118", "0491574632", "0491575254", "0491575789", "0491576398",
    "0491576801", "0491577426", "0491577644", "0491578957", "0491578148", "0491578888", "0491579212",
    "0491579760", "0491579455",
]

PHONE_FORMATS = {  # name -> regex used by verify.py
    "mobile_spaced": r"^04\d{2} \d{3} \d{3}$",          # 0491 570 156
    "mobile_intl": r"^\+61 4\d{2} \d{3} \d{3}$",        # +61 491 570 156
    "mobile_compact": r"^04\d{8}$",                      # 0491570156
    "landline_bracket": r"^\(0[2378]\) \d{4} \d{4}$",   # (02) 5550 1234
    "landline_intl_dash": r"^\+61-[2378]-\d{4}-\d{4}$", # +61-2-5550-1234
}


def landline(area):
    """ACMA fictional landline block: (0X) 5550 xxxx or (0X) 7010 xxxx -> 10 digits."""
    return f"{area}{rng.choice(['5550', '7010'])}{rng.randint(0, 9999):04d}"


def fmt_phone(digits, style):
    if digits.startswith("04"):
        d = digits
        if style == "mobile_spaced":
            return f"{d[:4]} {d[4:7]} {d[7:]}"
        if style == "mobile_intl":
            return f"+61 {d[1:4]} {d[4:7]} {d[7:]}"
        if style == "mobile_compact":
            return d
    else:
        area, a, b = digits[:2], digits[2:6], digits[6:]
        if style == "landline_bracket":
            return f"({area}) {a} {b}"
        if style == "landline_intl_dash":
            return f"+61-{area[1]}-{a}-{b}"
    raise ValueError(f"style {style} does not fit {digits}")


def other_style(digits, style):
    styles = ["mobile_spaced", "mobile_intl", "mobile_compact"] if digits.startswith("04") else [
        "landline_bracket", "landline_intl_dash"]
    return rng.choice([s for s in styles if s != style])


def mobile_number():
    return rng.choice(MOBILES)


def random_mobile_style():
    return rng.choice(["mobile_spaced", "mobile_spaced", "mobile_intl", "mobile_compact"])


def address():
    sub, st, pc, area = rng.choice(SUBURBS)
    unit = f"{rng.randint(1, 24)}/" if rng.random() < 0.25 else ""
    return {"street": f"{unit}{rng.randint(1, 240)} {rng.choice(STREETS)}", "city": sub,
            "state": st, "postcode": pc, "area": area}


def email_for(first, last, domain):
    return f"{first.lower()}.{last.lower()}@{domain}"


def recase_email(e):
    """Same address, different casing, e.g. jane.doe@example.com -> Jane.Doe@Example.COM.

    Note: Salesforce lowercases the standard Email field on save, so the casing is
    lost in the org; the duplicates still share the same address.
    """
    local, domain = e.split("@")
    parts = [p.capitalize() for p in local.split(".")]
    dom = domain.split(".")
    variant = ".".join(parts) + "@" + dom[0].capitalize() + "." + ".".join(x.upper() for x in dom[1:])
    assert variant != e and variant.lower() == e.lower()
    return variant


_used_names = set()


def unique_person():
    while True:
        f, l = rng.choice(FIRST), rng.choice(LAST)
        if (f, l) not in _used_names:
            _used_names.add((f, l))
            return f, l


DOMAINS = ["example.com", "example.net", "example.org"]

# --- accounts (40 landlords) --------------------------------------------------

accounts = []
landlord_people = []  # primary person behind each account
for i in range(40):
    key = f"SEED-A{i + 1:03d}"
    first, last = unique_person()
    while 16 <= i < 28 and any(a["Name"] == f"{last} Family Trust" for a in accounts):
        first, last = unique_person()
    if i < 16:
        name, kind = f"{first} {last}", "Individual"
    elif i < 28:
        name, kind = f"{last} Family Trust", "Family trust"
    else:
        name, kind = f"{COMPANY_WORDS[i - 28]} Holdings Pty Ltd", "Company"
    addr = address()
    acc = {
        "Seed_Key__c": key, "Name": name, "Description": f"Landlord ({kind})",
        "Phone": "", "Website": "",
        "BillingStreet": addr["street"], "BillingCity": addr["city"], "BillingStateCode": addr["state"],
        "BillingPostalCode": addr["postcode"], "BillingCountryCode": "AU",
    }
    if kind == "Individual" and rng.random() < 0.4:
        acc["Phone"] = fmt_phone(mobile_number(), random_mobile_style())
    else:
        acc["Phone"] = fmt_phone(landline(addr["area"]), rng.choice(["landline_bracket", "landline_intl_dash"]))
    if kind == "Company" and i % 2 == 0:
        acc["Website"] = f"www.{COMPANY_WORDS[i - 28].lower().replace(' ', '')}.example.com"
    accounts.append(acc)
    landlord_people.append((first, last, addr))

# Mess: blank phones on 6 accounts, blank addresses on 4 (no overlap needed)
for idx in rng.sample(range(40), 6):
    accounts[idx]["Phone"] = ""
for idx in rng.sample(range(40), 4):
    for f in ("BillingStreet", "BillingCity", "BillingStateCode", "BillingPostalCode", "BillingCountryCode"):
        accounts[idx][f] = ""

# --- contacts -----------------------------------------------------------------

contacts = []


def make_contact(key, first, last, account_key, role, addr):
    digits = mobile_number() if rng.random() < 0.75 else landline(addr["area"])
    style = random_mobile_style() if digits.startswith("04") else rng.choice(
        ["landline_bracket", "landline_intl_dash"])
    return {
        "Seed_Key__c": key, "FirstName": first, "LastName": last,
        "Email": email_for(first, last, rng.choice(DOMAINS)),
        "Phone": fmt_phone(digits, style), "_digits": digits, "_style": style,
        "Title": role, "Account.Seed_Key__c": account_key,
        "MailingStreet": addr["street"], "MailingCity": addr["city"], "MailingStateCode": addr["state"],
        "MailingPostalCode": addr["postcode"], "MailingCountryCode": "AU",
        "_role": role,
    }


# 42 landlord contacts: one per account, plus a second director on 2 companies
for i, (first, last, addr) in enumerate(landlord_people):
    if accounts[i]["Description"] == "Landlord (Individual)":
        f, l = first, last
    else:
        f, l = (first, last) if accounts[i]["Description"].endswith("trust)") else unique_person()
    contacts.append(make_contact(f"SEED-CL{len(contacts) + 1:03d}", f, l, accounts[i]["Seed_Key__c"], "Landlord", addr))
for i in (29, 33):
    f, l = unique_person()
    contacts.append(make_contact(f"SEED-CL{len(contacts) + 1:03d}", f, l, accounts[i]["Seed_Key__c"], "Landlord",
                                 landlord_people[i][2]))
landlord_contacts = list(contacts)
assert len(landlord_contacts) == 42

# --- opportunities (65 tenancies) ---------------------------------------------

STAGES = ["Closed Won"] * 45 + ["Prospecting"] * 8 + ["Negotiation/Review"] * 5 + ["Closed Lost"] * 7
rng.shuffle(STAGES)
opp_accounts = [a["Seed_Key__c"] for a in accounts] + [rng.choice(accounts)["Seed_Key__c"] for _ in range(25)]
rng.shuffle(opp_accounts)

# lease-date mess: 50 real dates, 10 text only, 5 neither
date_mode = ["date"] * 50 + ["text"] * 10 + ["none"] * 5
rng.shuffle(date_mode)
# property-state mess: 53 proper, 8 legacy, 4 blank
state_mode = ["proper"] * 53 + ["legacy"] * 8 + ["blank"] * 4
rng.shuffle(state_mode)
rent_blank = set(rng.sample(range(65), 3))
bond_blank = set(rng.sample([i for i in range(65) if i not in rent_blank], 7))
bond_six = set(rng.sample([i for i in range(65) if i not in rent_blank | bond_blank], 5))

TEXT_DATE_FORMATS = [
    lambda d: f"{d.day}/{d.month:02d}/{d.year}",       # 3/02/2024
    lambda d: d.isoformat(),                            # 2024-02-03
    lambda d: d.strftime("%b ") + f"{d.day} {d.year}",  # Feb 3 2024
    lambda d: d.strftime("%d.%m.%y"),                   # 03.02.24
]

opps = []
for i in range(65):
    key = f"SEED-O{i + 1:03d}"
    sub, st, pc, _ = rng.choice([s for s in SUBURBS if s[1] in LEGACY_STATE] if state_mode[i] == "legacy" else SUBURBS)
    street = f"{rng.randint(1, 24)}/{rng.randint(1, 240)}" if rng.random() < 0.3 else f"{rng.randint(1, 240)}"
    prop = f"{street} {rng.choice(STREETS)}, {sub} {st} {pc}"
    lease_start = date(2022, 1, 1) + timedelta(days=rng.randint(0, 1400))
    rent = rng.randrange(380, 1455, 5)
    o = {
        "Seed_Key__c": key, "Account.Seed_Key__c": opp_accounts[i],
        "Name": f"{prop} - Lease {lease_start.year}",
        "StageName": STAGES[i], "CloseDate": (lease_start - timedelta(days=rng.randint(5, 30))).isoformat(),
        "Description": f"Property: {prop}",
        "Weekly_Rent__c": "" if i in rent_blank else f"{rent:.2f}",
        "Bond__c": "" if i in bond_blank or i in rent_blank else f"{rent * (6 if i in bond_six else 4):.2f}",
        "Lease_Start_Date__c": lease_start.isoformat() if date_mode[i] == "date" else "",
        "Lease_Start_Text__c": rng.choice(TEXT_DATE_FORMATS)(lease_start) if date_mode[i] == "text" else "",
        "Property_State__c": {"proper": st, "legacy": LEGACY_STATE.get(st, st), "blank": ""}[state_mode[i]],
        "_lease_start": lease_start,
    }
    opps.append(o)

# --- tenants (38) linked to tenancies -----------------------------------------

won = [o for o in opps if o["StageName"] == "Closed Won"]           # 45
tenant_slots = list(won)                                            # 1 primary tenant per signed lease
rng.shuffle(tenant_slots)
tenants = []
contact_roles = []  # (opp key, contact key, role, is_primary)
for t in range(38):
    first, last = unique_person()
    opp = tenant_slots[t]
    addr = address()
    account_key = opp["Account.Seed_Key__c"] if t < 26 else ""     # 12 tenants with no account
    c = make_contact(f"SEED-CT{t + 1:03d}", first, last, account_key, "Tenant", addr)
    tenants.append(c)
    contact_roles.append((opp["Seed_Key__c"], c["Seed_Key__c"], "Tenant", True))
# remaining 7 signed leases: a returning tenant moved/renewed into another property
for opp in tenant_slots[38:]:
    c = rng.choice(tenants)
    contact_roles.append((opp["Seed_Key__c"], c["Seed_Key__c"], "Tenant", True))
# 10 co-tenants (secondary role) on signed leases
for opp in rng.sample(won, 10):
    already = {ck for ok, ck, _, _ in contact_roles if ok == opp["Seed_Key__c"]}
    c = rng.choice([x for x in tenants if x["Seed_Key__c"] not in already])
    contact_roles.append((opp["Seed_Key__c"], c["Seed_Key__c"], "Tenant", False))
contacts.extend(tenants)

# Mess: blank email on 8 and blank phone on 8 originals (dup sources keep their email)
dup_sources = rng.sample(landlord_contacts, 10) + rng.sample(tenants, 5)
dup_source_keys = {c["Seed_Key__c"] for c in dup_sources}
eligible = [c for c in contacts if c["Seed_Key__c"] not in dup_source_keys]
for c in rng.sample(eligible, 8):
    c["Email"] = ""
for c in rng.sample(contacts, 8):
    c["Phone"] = ""

# 15 duplicates: same person, email differs only by case, phone in another format
dups = []
for n, src in enumerate(dup_sources):
    d = dict(src)
    d["Seed_Key__c"] = f"SEED-CD{n + 1:03d}"
    d["Email"] = recase_email(src["Email"])
    d["Phone"] = fmt_phone(src["_digits"], other_style(src["_digits"], src["_style"]))
    if n in (3, 8, 13):  # 3 duplicates with no account
        d["Account.Seed_Key__c"] = ""
    dups.append(d)
contacts.extend(dups)
assert len(contacts) == 95

# --- tasks (50) ---------------------------------------------------------------

SUBJECTS = ["Routine inspection", "Rent arrears follow-up", "Lease renewal call", "Bond lodgement check",
            "Entry condition report", "Maintenance request - leaking tap", "Smoke alarm compliance check",
            "Send rent increase notice"]
STATUSES = ["Not Started", "In Progress", "Completed", "Completed", "Completed", "Waiting on someone else", "Deferred"]
primary_tenant = {ok: ck for ok, ck, _, prim in contact_roles if prim}
task_opps = [o for o in opps if o["Seed_Key__c"] in primary_tenant]
tasks = []
no_who = set(rng.sample(range(50), 6))
no_date = set(rng.sample(range(50), 8))
for i in range(50):
    o = rng.choice(task_opps)
    due = o["_lease_start"] + timedelta(days=rng.randint(7, 400))
    subj = rng.choice(SUBJECTS)
    desc = ""
    if rng.random() < 0.3:
        desc = f"Tenant said they'd call back after {due.day}/{due.month}/{str(due.year)[2:]}"
    tasks.append({
        "Seed_Key__c": f"SEED-T{i + 1:03d}", "Subject": subj,
        "WhatKey": o["Seed_Key__c"], "WhoKey": "" if i in no_who else primary_tenant[o["Seed_Key__c"]],
        "ActivityDate": "" if i in no_date else due.isoformat(),
        "Status": rng.choice(STATUSES), "Priority": "High" if "arrears" in subj else "Normal",
        "Description": desc,
    })

# --- harder duplicates (6) ----------------------------------------------------
# Generated last so they don't shift the random sequence for the records above.

NICKNAMES = {"William": "Bill", "Henry": "Harry", "Matilda": "Tilly", "Oliver": "Ollie", "Charlotte": "Lottie",
             "Lachlan": "Lachie", "Imogen": "Immy", "Harrison": "Harry", "Archie": "Archibald", "Leo": "Leonard"}
TYPO_DOMAINS = {"example.com": "example.cmo", "example.net": "example.nte", "example.org": "exmaple.ogr"}
# Typo'd endings are not real top-level domains, so these addresses can never resolve.

hard_pool = [c for c in contacts[:80] if c["Seed_Key__c"] not in dup_source_keys
             and c["Account.Seed_Key__c"] and c["Email"] and c["Phone"]]
hard = []


def add_hard(src, **changes):
    d = dict(src)
    d.update(changes)
    d["Seed_Key__c"] = f"SEED-CX{len(hard) + 1:03d}"
    hard.append(d)
    hard_pool.remove(src)


# 2: nickname / full-name variant, same phone, different email
for src in rng.sample([c for c in hard_pool if c["FirstName"] in NICKNAMES], 2):
    nick = NICKNAMES[src["FirstName"]]
    add_hard(src, FirstName=nick, Email=email_for(nick, src["LastName"], rng.choice(DOMAINS)))
assert len(hard) == 2, "not enough contacts with a nickname-able first name"

# 2: same name, mistyped email domain, phone in another format
for src in rng.sample(hard_pool, 2):
    local, dom = src["Email"].split("@")
    add_hard(src, Email=f"{local}@{TYPO_DOMAINS[dom]}",
             Phone=fmt_phone(src["_digits"], other_style(src["_digits"], src["_style"])))

# 2: same name and email, but filed under a different landlord account
for src in rng.sample(hard_pool, 2):
    other = rng.choice([a["Seed_Key__c"] for a in accounts if a["Seed_Key__c"] != src["Account.Seed_Key__c"]])
    add_hard(src, **{"Account.Seed_Key__c": other})

contacts.extend(hard)
assert len(contacts) == 101

# --- write --------------------------------------------------------------------


def write(name, rows, cols):
    OUT.mkdir(exist_ok=True)
    with open(OUT / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore", lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)


write("accounts.csv", accounts, list(accounts[0].keys()))
write("contacts.csv", contacts, [k for k in contacts[0] if not k.startswith("_")])
write("opportunities.csv", opps, [k for k in opps[0] if not k.startswith("_")])
write("tasks_keys.csv", tasks, list(tasks[0].keys()))
write("contact_roles_keys.csv",
      [{"OppKey": o, "ContactKey": c, "Role": r, "IsPrimary": str(p).lower()} for o, c, r, p in contact_roles],
      ["OppKey", "ContactKey", "Role", "IsPrimary"])


def classify(phone):
    if not phone:
        return "blank"
    for name, rx in PHONE_FORMATS.items():
        if re.match(rx, phone):
            return name
    return "unrecognised"


phones = Counter(classify(r["Phone"]) for r in accounts + contacts)
assert "unrecognised" not in phones and all(phones[f] > 0 for f in PHONE_FORMATS), phones
email_groups = Counter(c["Email"].lower() for c in contacts if c["Email"])
metric_rows = [{"first": c["FirstName"], "last": c["LastName"], "email": c["Email"], "phone": c["Phone"],
                "account": c["Account.Seed_Key__c"]} for c in contacts]

manifest = {
    "totals": {"Account": len(accounts), "Contact": len(contacts), "Opportunity": len(opps), "Task": len(tasks),
               "OpportunityContactRole": len(contact_roles)},
    "pre_existing": {"Account": 4, "Contact": 4},
    "phone_formats": dict(sorted(phones.items())),
    "phone_format_regex": PHONE_FORMATS,
    "contacts_no_account": sum(1 for c in contacts if not c["Account.Seed_Key__c"]),
    "contacts_blank_email": sum(1 for c in contacts if not c["Email"]),
    "contacts_blank_phone": sum(1 for c in contacts if not c["Phone"]),
    "duplicate_email_pairs": sum(1 for v in email_groups.values() if v == 2),
    "hard_duplicates": {
        "name_variant_same_phone": metrics.name_variant_same_phone(metric_rows),
        "typo_domain_same_name": metrics.typo_domain_same_name(metric_rows),
        "same_email_different_account": metrics.same_email_different_account(metric_rows),
    },
    "accounts_blank_phone": sum(1 for a in accounts if not a["Phone"]),
    "accounts_blank_address": sum(1 for a in accounts if not a["BillingCity"]),
    "opps_by_stage": dict(sorted(Counter(o["StageName"] for o in opps).items())),
    "opps_lease_date": sum(1 for o in opps if o["Lease_Start_Date__c"]),
    "opps_lease_text_only": sum(1 for o in opps if o["Lease_Start_Text__c"] and not o["Lease_Start_Date__c"]),
    "opps_lease_neither": sum(1 for o in opps if not o["Lease_Start_Text__c"] and not o["Lease_Start_Date__c"]),
    "opps_state_legacy": sum(1 for o in opps if o["Property_State__c"] in LEGACY_STATE.values()),
    "opps_state_blank": sum(1 for o in opps if not o["Property_State__c"]),
    "legacy_state_values": sorted(LEGACY_STATE.values()),
    "opps_rent_blank": sum(1 for o in opps if not o["Weekly_Rent__c"]),
    "opps_bond_blank": sum(1 for o in opps if not o["Bond__c"]),
    "tasks_no_who": sum(1 for t in tasks if not t["WhoKey"]),
    "tasks_no_due_date": sum(1 for t in tasks if not t["ActivityDate"]),
}
(OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print(json.dumps(manifest, indent=2))
