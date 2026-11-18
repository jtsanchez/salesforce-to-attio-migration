"""Duplicate-contact rules from mapping.yaml `dedupe` (migrate/dedupe.py)."""
import dedupe as dd

NICK = {"harry": ["henry", "harrison"], "tilly": ["matilda"], "bill": ["william"]}
CONFIG = {"nicknames": NICK, "typo_email_cleanup": {"known_good_min_contacts": 2, "max_distance": 2}}


def contact(cid, first, last, email=None, phone=None, account="001A", created="2026-11-16T00:00:00.000+0000",
            title="Landlord"):
    return {"Id": cid, "FirstName": first, "LastName": last, "Email": email, "Phone": phone,
            "AccountId": account, "CreatedDate": created, "Title": title}


def groups_by_ids(contacts):
    groups, _, ambiguous = dd.find_groups(contacts, NICK)
    return sorted(sorted(c["Id"] for c in g) for g in groups), ambiguous


def test_exact_email_is_case_insensitive():
    cs = [contact("1", "Jane", "Doe", "jane.doe@example.com"),
          contact("2", "Jane", "Doe", "Jane.Doe@Example.COM", account="001B")]
    assert groups_by_ids(cs)[0] == [["1", "2"]]


def test_same_name_and_phone_in_different_formats():
    cs = [contact("1", "Aisha", "Novak", "aisha.novak@example.org", "+61-7-7010-5761"),
          contact("2", "Aisha", "Novak", "aisha.novak@exmaple.ogr", "(07) 7010 5761")]
    assert groups_by_ids(cs)[0] == [["1", "2"]]


def test_same_name_different_phone_is_not_merged():
    cs = [contact("1", "Sam", "Lee", phone="0491 570 156"), contact("2", "Sam", "Lee", phone="0491 570 157")]
    assert groups_by_ids(cs)[0] == []


def test_phone_alone_never_merges():
    cs = [contact("1", "Sam", "Lee", phone="0491 570 156"), contact("2", "Ann", "Ward", phone="0491570156")]
    assert groups_by_ids(cs)[0] == []


def test_nickname_with_same_phone_merges():
    cs = [contact("1", "Matilda", "Chandra", "m@example.com", "0491 579 212"),
          contact("2", "Tilly", "Chandra", "t@example.com", "+61 491 579 212")]
    assert groups_by_ids(cs)[0] == [["1", "2"]]


def test_nickname_without_shared_phone_is_not_merged():
    cs = [contact("1", "Matilda", "Chandra", phone="0491 579 212"),
          contact("2", "Tilly", "Chandra", phone="0491 570 006")]
    assert groups_by_ids(cs)[0] == []


def test_ambiguous_nickname_is_flagged_and_never_chains_two_people():
    cs = [contact("1", "Henry", "Nakamura", phone="0491 573 770"),
          contact("2", "Harrison", "Nakamura", phone="0491 573 770"),
          contact("3", "Harry", "Nakamura", phone="+61 491 573 770")]
    groups, ambiguous = groups_by_ids(cs)
    assert groups == []                       # Henry and Harrison must not end up together via Harry
    assert [c["Id"] for c, _ in ambiguous] == ["3"]


def test_nickname_with_one_matching_candidate_merges_despite_other_full_name():
    # Real extract: Harrison Nakamura exists but has no phone, so Harry only matches Henry.
    cs = [contact("1", "Henry", "Nakamura", phone="0491 573 770"),
          contact("2", "Harrison", "Nakamura", phone=None),
          contact("3", "Harry", "Nakamura", phone="+61 491 573 770")]
    groups, ambiguous = groups_by_ids(cs)
    assert groups == [["1", "3"]] and ambiguous == []


def test_survivor_is_oldest_then_lowest_id():
    a = contact("003B", "Jane", "Doe", "jane@example.com", created="2026-11-16T00:00:00.000+0000")
    b = contact("003A", "Jane", "Doe", "jane@example.com", created="2026-11-16T00:00:00.000+0000")
    c = contact("003C", "Jane", "Doe", "jane@example.com", created="2026-11-15T00:00:00.000+0000")
    assert dd.choose_survivor([a, b])["Id"] == "003A"
    assert dd.choose_survivor([a, b, c])["Id"] == "003C"


def test_merge_keeps_full_first_name_and_unions_contact_details():
    cs = [contact("1", "Tilly", "Chandra", "tilly@example.com", "0491 579 212", created="2026-11-15T00:00:00.000+0000"),
          contact("2", "Matilda", "Chandra", "matilda@example.com", "+61 491 579 212")]
    people, merge_map, merges, _ = dd.dedupe(cs, CONFIG)
    assert len(people) == 1 and merge_map == {"2": "1"}
    person = people[0]
    assert person["contact"]["FirstName"] == "Matilda"         # full name beats the nickname
    assert person["emails"] == ["tilly@example.com", "matilda@example.com"]
    assert merges[0]["phones"] == ["+61491579212"]               # same number in two formats -> one
    assert merges[0]["rules"] == ["nickname_same_phone"]


def test_merge_drops_typo_email_and_records_it():
    # example.net is used by 2 contacts (known-good at min 2); example.nte by only 1.
    cs = [contact("1", "Thi", "Varga", "thi.varga@example.net", "0491572549"),
          contact("2", "Thi", "Varga", "thi.varga@example.nte", "0491 572 549"),
          contact("3", "Sam", "Lee", "sam.lee@example.net", "0491 570 006")]
    people, _, merges, _ = dd.dedupe(cs, CONFIG)
    thi = next(p for p in people if p["contact"]["LastName"] == "Varga")
    assert thi["emails"] == ["thi.varga@example.net"]
    assert merges[0]["dropped_emails"] == [("thi.varga@example.nte", "thi.varga@example.net")]


def test_rare_domain_is_not_dropped_without_a_known_good_near_match():
    # A domain used by a single contact isn't treated as a typo unless a known-good near-match exists.
    kept, dropped = dd.clean_typo_emails(["a@agency.com.au", "a@example.com"], {"example.com"}, 2)
    assert kept == ["a@agency.com.au", "a@example.com"] and dropped == []


def test_typo_cleanup_keeps_two_genuinely_different_good_domains():
    kept, dropped = dd.clean_typo_emails(["a@example.com", "a@example.net"], {"example.com", "example.net"}, 2)
    assert kept == ["a@example.com", "a@example.net"] and dropped == []


def test_merge_records_other_account():
    cs = [contact("1", "Rohan", "Abernethy", "rohan@example.net", account="001A"),
          contact("2", "Rohan", "Abernethy", "rohan@example.net", account="001B")]
    _, _, merges, _ = dd.dedupe(cs, CONFIG)
    assert merges[0]["other_account_ids"] == ["001B"]
