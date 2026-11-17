"""Duplicate-detection metrics shared by generate.py (expected) and verify.py (actual).

Each function takes contacts as dicts with keys: first, last, email, phone, account.
"""
from collections import defaultdict
from itertools import combinations

GOOD_DOMAINS = {"example.com", "example.net", "example.org"}


def name_variant_same_phone(contacts):
    """Pairs with the same last name and phone but a different first name (Bill vs William)."""
    groups = defaultdict(list)
    for c in contacts:
        if c["phone"]:
            groups[(c["last"].lower(), c["phone"])].append(c["first"].lower())
    return sum(1 for firsts in groups.values() for a, b in combinations(firsts, 2) if a != b)


def typo_domain_same_name(contacts):
    """Contacts with a mistyped email domain who share a full name with another contact."""
    names = defaultdict(int)
    for c in contacts:
        names[(c["first"].lower(), c["last"].lower())] += 1
    return sum(1 for c in contacts
               if c["email"] and c["email"].lower().split("@")[1] not in GOOD_DOMAINS
               and names[(c["first"].lower(), c["last"].lower())] > 1)


def same_email_different_account(contacts):
    """Email addresses held by contacts on more than one (non-blank) account."""
    accounts = defaultdict(set)
    for c in contacts:
        if c["email"] and c["account"]:
            accounts[c["email"].lower()].add(c["account"])
    return sum(1 for accs in accounts.values() if len(accs) > 1)
