"""Duplicate-contact grouping and merging per migrate/mapping.yaml `dedupe`. Pure, no I/O.

Rules (union-find, so the order they run in doesn't change the groups):
  same_email            email equal, case-insensitive (Attio requires this one)
  same_name_same_phone  same first + last name and a shared E.164 phone
  nickname_same_phone   same last name, shared E.164 phone, nickname pair; ambiguous nicknames are
                        flagged and never merged
Every merge is reported so it can be reviewed (reviewer decision: merge and flag all).
"""
from collections import Counter, defaultdict
from itertools import combinations

from normalise import osa_distance, phone_e164


class UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra
            return True
        return False


def _phones(c):
    p, _ = phone_e164(c.get("Phone"))
    return {p} if p else set()


def _canon_names(first, nicknames):
    """Full names a first name can stand for (itself included)."""
    f = (first or "").lower()
    return {f} | set(nicknames.get(f, []))


def find_groups(contacts, nicknames):
    """Return (groups, rule_hits, ambiguous).

    groups: list of lists of contacts with more than one member.
    rule_hits: {frozenset of pair ids: rule id} for every merging pair, for the report.
    ambiguous: list of (contact, [candidate contacts]) where a nickname matched more than one person.
    """
    by_id = {c["Id"]: c for c in contacts}
    uf = UnionFind(by_id)
    hits = {}

    def link(a, b, rule):
        if uf.union(a["Id"], b["Id"]):
            hits[frozenset((a["Id"], b["Id"]))] = rule

    # same_email
    by_email = defaultdict(list)
    for c in contacts:
        if c.get("Email"):
            by_email[c["Email"].strip().lower()].append(c)
    for grp in by_email.values():
        for other in grp[1:]:
            link(grp[0], other, "same_email")

    # same_name_same_phone
    by_name = defaultdict(list)
    for c in contacts:
        by_name[((c.get("FirstName") or "").strip().lower(), (c.get("LastName") or "").strip().lower())].append(c)
    for grp in by_name.values():
        for a, b in combinations(grp, 2):
            if _phones(a) & _phones(b):
                link(a, b, "same_name_same_phone")

    # nickname_same_phone, with the ambiguity guard
    ambiguous = []
    by_last = defaultdict(list)
    for c in contacts:
        by_last[(c.get("LastName") or "").strip().lower()].append(c)
    for grp in by_last.values():
        for c in grp:
            first = (c.get("FirstName") or "").lower()
            if first not in nicknames:
                continue
            candidates = [o for o in grp if o is not c and (o.get("FirstName") or "").lower() != first
                          and (o.get("FirstName") or "").lower() in nicknames[first]]
            with_phone = [o for o in candidates if _phones(o) & _phones(c)]
            distinct_people = {(o.get("FirstName") or "").lower() for o in with_phone}
            if len(distinct_people) > 1:
                ambiguous.append((c, with_phone))
            elif with_phone:
                for o in with_phone:
                    link(o, c, "nickname_same_phone")

    members = defaultdict(list)
    for c in contacts:
        members[uf.find(c["Id"])].append(c)
    groups = [g for g in members.values() if len(g) > 1]
    return groups, hits, ambiguous


def known_good_domains(contacts, min_contacts):
    counts = Counter(c["Email"].lower().split("@", 1)[1] for c in contacts if c.get("Email") and "@" in c["Email"])
    return {d for d, n in counts.items() if n >= min_contacts}


def clean_typo_emails(emails, good_domains, max_distance):
    """Drop emails whose domain is a near-miss of a known-good domain on the same local part.

    Returns (kept emails in input order, dropped [(typo, matched good address)]).
    """
    kept, dropped = [], []
    for e in emails:
        local, dom = e.split("@", 1)
        if dom not in good_domains:
            match = next((g for g in emails if g != e and g.split("@", 1)[0] == local
                          and g.split("@", 1)[1] in good_domains
                          and osa_distance(dom, g.split("@", 1)[1]) <= max_distance), None)
            if match:
                dropped.append((e, match))
                continue
        kept.append(e)
    return kept, dropped


def choose_survivor(group):
    """Oldest CreatedDate, ties broken by lowest Id."""
    return sorted(group, key=lambda c: (c.get("CreatedDate") or "", c["Id"]))[0]


def merge_group(group, nicknames, good_domains, max_distance):
    """Merge a duplicate group into one survivor-shaped contact plus merge details."""
    survivor = choose_survivor(group)
    ordered = [survivor] + sorted((c for c in group if c is not survivor),
                                  key=lambda c: (c.get("CreatedDate") or "", c["Id"]))
    merged = dict(survivor)
    for field in ("LastName", "MailingStreet", "MailingCity", "MailingStateCode", "MailingPostalCode",
                  "MailingCountryCode", "AccountId", "Title"):
        if not merged.get(field):
            merged[field] = next((c.get(field) for c in ordered if c.get(field)), None)

    # first name: the full name beats a nickname (Matilda over Tilly)
    firsts = [c.get("FirstName") for c in ordered if c.get("FirstName")]
    full = [f for f in firsts if f.lower() not in nicknames]
    merged["FirstName"] = full[0] if full else (firsts[0] if firsts else None)

    emails = list(dict.fromkeys(c["Email"].strip().lower() for c in ordered if c.get("Email")))
    emails, dropped_emails = clean_typo_emails(emails, good_domains, max_distance)
    phones = list(dict.fromkeys(p for c in ordered for p in _phones(c)))
    titles = list(dict.fromkeys(c["Title"] for c in ordered if c.get("Title")))
    accounts = list(dict.fromkeys(c["AccountId"] for c in ordered if c.get("AccountId")))

    return merged, {
        "survivor_id": survivor["Id"],
        "loser_ids": [c["Id"] for c in ordered[1:]],
        "emails": emails,
        "dropped_emails": dropped_emails,
        "phones": phones,
        "titles": titles,
        "other_account_ids": [a for a in accounts if a != merged.get("AccountId")],
        "names": list(dict.fromkeys(f"{c.get('FirstName') or ''} {c.get('LastName') or ''}".strip() for c in ordered)),
    }


def dedupe(contacts, config):
    """Run all rules and merges. Returns (people_sources, merge_map, merges, ambiguous).

    people_sources: one dict per output person: {"contact": merged contact, "merge": details | None}
    merge_map: loser Id -> survivor Id
    merges: list of merge details with the rules that joined each group
    """
    nicknames = {k.lower(): [v.lower() for v in vals] for k, vals in config["nicknames"].items()}
    cleanup = config["typo_email_cleanup"]
    good = known_good_domains(contacts, cleanup["known_good_min_contacts"])
    groups, hits, ambiguous = find_groups(contacts, nicknames)

    merge_map, merges, in_group = {}, [], set()
    people = []
    for g in groups:
        merged, details = merge_group(g, nicknames, good, cleanup["max_distance"])
        ids = {c["Id"] for c in g}
        details["rules"] = sorted({rule for pair, rule in hits.items() if pair <= ids})
        merges.append(details)
        for loser in details["loser_ids"]:
            merge_map[loser] = details["survivor_id"]
        in_group |= ids
        people.append({"contact": merged, "merge": details})
    for c in contacts:
        if c["Id"] not in in_group:
            emails = [c["Email"].strip().lower()] if c.get("Email") else []
            people.append({"contact": dict(c), "merge": None, "emails": emails})
    for p in people:
        if p["merge"]:
            p["emails"] = p["merge"]["emails"]
    people.sort(key=lambda p: p["contact"]["Id"])
    return people, merge_map, merges, ambiguous
