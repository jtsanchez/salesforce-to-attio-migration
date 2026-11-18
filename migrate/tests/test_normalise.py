"""Normalisation rules from mapping.yaml `transforms` (migrate/normalise.py)."""
from datetime import date

import pytest

import normalise as nz

TODAY = date(2026, 11, 18)


# --- phones -------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("0491 570 156", "+61491570156"),        # mobile, spaced
    ("+61 491 570 156", "+61491570156"),     # mobile, international
    ("0491570156", "+61491570156"),          # mobile, compact
    ("(02) 5550 1234", "+61255501234"),      # landline, bracketed area code
    ("+61-2-5550-1234", "+61255501234"),     # landline, international with dashes
    ("61491570156", "+61491570156"),         # 61 without the plus
    ("(08) 7010 0319", "+61870100319"),      # other area codes
])
def test_phone_all_source_formats(raw, expected):
    value, how = nz.phone_e164(raw)
    assert value == expected
    assert how == "normalised"


def test_phone_already_e164_is_unchanged():
    assert nz.phone_e164("+61491570156") == ("+61491570156", "unchanged")


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_phone_blank(raw):
    assert nz.phone_e164(raw) == (None, "blank")


@pytest.mark.parametrize("raw", ["12345", "0491 570", "+44 20 7946 0958", "0591 570 156", "call reception", "1800 160 401"])
def test_phone_unparseable(raw):
    assert nz.phone_e164(raw) == (None, "unparseable")


# --- states ---------------------------------------------------------------------

@pytest.mark.parametrize("legacy, code", [
    ("Victoria", "VIC"), ("N.S.W.", "NSW"), ("Queensland", "QLD"), ("W.A.", "WA"), ("Sth Aust", "SA"),
    ("victoria", "VIC"), ("New South Wales", "NSW"), ("Tasmania", "TAS"),
])
def test_state_legacy_values_map_to_codes(aliases, legacy, code):
    assert nz.state_normalise(legacy, code, aliases) == (code, "alias")


@pytest.mark.parametrize("code", ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"])
def test_state_codes_pass_through(aliases, code):
    assert nz.state_normalise(code, code, aliases) == (code, "code")


def test_state_lowercase_code_counts_as_alias(aliases):
    assert nz.state_normalise("qld", "QLD", aliases) == ("QLD", "alias")


def test_state_blank_is_filled_from_address(aliases):
    assert nz.state_normalise(None, "TAS", aliases) == ("TAS", "from_address")
    assert nz.state_normalise("  ", "NT", aliases) == ("NT", "from_address")


def test_state_blank_without_address_is_missing(aliases):
    assert nz.state_normalise(None, None, aliases) == (None, "missing")


def test_state_conflict_address_wins(aliases):
    assert nz.state_normalise("Victoria", "NSW", aliases) == ("NSW", "conflict")


def test_state_unknown_value_falls_back_to_address(aliases):
    assert nz.state_normalise("Victoriaa", "VIC", aliases) == ("VIC", "unknown")
    assert nz.state_normalise("Victoriaa", None, aliases) == (None, "unknown")


def test_mapping_aliases_cover_every_legacy_value_in_the_seed(aliases):
    for legacy in ("Victoria", "N.S.W.", "Queensland", "W.A.", "Sth Aust"):
        assert legacy.lower() in aliases


# --- lease dates ------------------------------------------------------------------

def test_lease_date_field_wins_over_text():
    assert nz.lease_start("2024-06-15", "1/01/2020", TODAY) == ("2024-06-15", "date_field")


@pytest.mark.parametrize("text, expected, fmt", [
    ("2024-02-03", "2024-02-03", "iso"),
    ("3/02/2024", "2024-02-03", "d/mm/yyyy"),      # day-first: 3 Feb, not 2 Mar
    ("13/02/2024", "2024-02-13", "d/mm/yyyy"),
    ("03.02.24", "2024-02-03", "dd.mm.yy"),
    ("24.07.23", "2023-07-24", "dd.mm.yy"),
    ("Feb 3 2024", "2024-02-03", "mon d yyyy"),
    ("Dec 14 2023", "2023-12-14", "mon d yyyy"),
    ("February 3, 2024", "2024-02-03", "mon d yyyy"),
])
def test_lease_text_formats(text, expected, fmt):
    assert nz.lease_start(None, text, TODAY) == (expected, f"text:{fmt}")


def test_lease_month_first_is_never_assumed():
    # US-style 02/13/2024 would be 13 Feb; read day-first it is month 13, so it is rejected.
    assert nz.lease_start(None, "02/13/2024", TODAY) == (None, "unparseable")


@pytest.mark.parametrize("text", ["31/02/2024", "30.02.24", "Feb 30 2024", "Fbr 3 2024", "next spring", "2024/02/03"])
def test_lease_unparseable(text):
    assert nz.lease_start(None, text, TODAY) == (None, "unparseable")


@pytest.mark.parametrize("text", ["1/01/1999", "2014-12-31", "1/01/2030"])
def test_lease_outside_sanity_window(text):
    assert nz.lease_start(None, text, TODAY) == (None, "out_of_range")


def test_lease_sanity_window_edges():
    assert nz.lease_start(None, "2015-01-01", TODAY)[0] == "2015-01-01"
    assert nz.lease_start(None, "2027-11-18", TODAY)[0] == "2027-11-18"   # today + 365 days
    assert nz.lease_start(None, "2027-11-19", TODAY) == (None, "out_of_range")


def test_lease_date_field_out_of_range():
    assert nz.lease_start("1990-01-01", None, TODAY) == (None, "out_of_range")


@pytest.mark.parametrize("text", [None, "", "  "])
def test_lease_missing(text):
    assert nz.lease_start(None, text, TODAY) == (None, "missing")


# --- addresses ---------------------------------------------------------------------

def test_address_parses_unit_numbers_and_multiword_suburbs(mapping):
    pattern = mapping["transforms"]["address_from_deal_name"]["regex"]
    loc, how = nz.address_from_deal_name("22/215 Banksia St, Fortitude Valley QLD 4006 - Lease 2025", pattern)
    assert how == "parsed"
    assert loc == {"line_1": "22/215 Banksia St", "line_2": None, "line_3": None, "line_4": None,
                   "locality": "Fortitude Valley", "region": "QLD", "postcode": "4006", "country_code": "AU",
                   "latitude": None, "longitude": None}


def test_address_parses_nt_postcode_with_leading_zero(mapping):
    pattern = mapping["transforms"]["address_from_deal_name"]["regex"]
    loc, _ = nz.address_from_deal_name("7 Myrtle Tce, Parap NT 0820 - Lease 2024", pattern)
    assert (loc["locality"], loc["region"], loc["postcode"]) == ("Parap", "NT", "0820")


@pytest.mark.parametrize("name", ["Lease renewal for unit 4", "14 Banksia St Marrickville NSW 2204", ""])
def test_address_unparseable(mapping, name):
    pattern = mapping["transforms"]["address_from_deal_name"]["regex"]
    assert nz.address_from_deal_name(name, pattern) == (None, "unparseable")


def test_location_object_has_all_ten_keys_or_is_none():
    loc = nz.location_object("63 Kurrajong St", "Marrickville", "NSW", "2204", "AU")
    assert set(loc) == set(nz.LOCATION_KEYS)
    assert loc["region"] == "NSW" and loc["line_2"] is None
    assert nz.location_object(None, "", None, None, None) is None


# --- small field rules ---------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("www.harbourline.example.com", "harbourline.example.com"),
    ("https://WWW.Saltbush.example.com/about", "saltbush.example.com"),
    ("tidewater.example.com", "tidewater.example.com"),
    ("", None), (None, None),
])
def test_domain_from_website(raw, expected):
    assert nz.domain_from_website(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Landlord (Individual)", "Individual"), ("Landlord (Family trust)", "Family trust"),
    ("Landlord (Company)", "Company"), ("Household", None), (None, None),
])
def test_landlord_type(raw, expected):
    assert nz.landlord_type_from_description(raw) == expected


def test_sf_datetime_to_utc():
    assert nz.sf_datetime("2026-11-16T00:55:27.000+0000") == "2026-11-16T00:55:27Z"
    assert nz.sf_datetime("2026-11-16T10:55:27.000+1000") == "2026-11-16T00:55:27Z"
    assert nz.sf_datetime(None) is None


def test_task_deadline_is_9am_aest():
    assert nz.task_deadline("2025-06-25") == "2025-06-25T09:00:00+10:00"
    assert nz.task_deadline(None) is None


def test_task_content_priority_description_and_status():
    assert nz.task_content("High", "Rent arrears follow-up", "Call back after 19/8/24", "Deferred") == \
        "[High] Rent arrears follow-up\n\nCall back after 19/8/24\n[SF status: Deferred]"
    assert nz.task_content("Normal", "Routine inspection", None, "Completed") == "Routine inspection"
    assert nz.task_content("Normal", "Routine inspection", "  ", "Not Started") == "Routine inspection"


def test_task_content_is_truncated_to_attio_limit():
    assert len(nz.task_content("Normal", "x" * 3000, None, "Completed")) == 2000


@pytest.mark.parametrize("a, b, dist", [
    ("example.cmo", "example.com", 1),   # one swap
    ("example.nte", "example.net", 1),
    ("exmaple.ogr", "example.org", 2),   # two swaps: plain edit distance would say 4
    ("example.com", "example.net", 3),
    ("example.org", "example.org", 0),
])
def test_osa_distance_counts_swaps_as_one_edit(a, b, dist):
    assert nz.osa_distance(a, b) == dist
