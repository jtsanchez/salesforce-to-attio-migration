"""End-to-end dry run of migrate/transform.py on the real extract, with network access blocked."""
import ast
import json
import socket
import subprocess
from pathlib import Path

import pytest

import transform

MIGRATE = Path(__file__).resolve().parents[1]
RAW = MIGRATE.parent / "data" / "raw"
FORBIDDEN_IMPORTS = {"socket", "subprocess", "urllib", "http", "requests", "httpx", "sfcli", "ssl"}

needs_extract = pytest.mark.skipif(not (RAW / "manifest.json").exists(),
                                   reason="data/raw not present; run migrate/extract_sf.py first")


@pytest.mark.parametrize("module", ["transform.py", "normalise.py", "dedupe.py"])
def test_transform_modules_import_nothing_that_can_reach_the_network(module):
    tree = ast.parse((MIGRATE / module).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & FORBIDDEN_IMPORTS


@pytest.fixture
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("transform attempted network or subprocess access")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


@pytest.fixture
def dry_run(tmp_path, no_network):
    out = tmp_path / "transformed"
    code = transform.main(["--raw", str(RAW), "--out", str(out), "--today", "2026-11-18"])
    load = lambda name: json.loads((out / f"{name}.json").read_text(encoding="utf-8"))  # noqa: E731
    return code, out, {n: load(n) for n in ("companies", "people", "deals", "tasks")}


@needs_extract
def test_dry_run_matches_expected_counts(dry_run, mapping):
    code, out, data = dry_run
    assert code == 0
    exp = mapping["expected_counts"]
    for name in ("companies", "people", "deals", "tasks"):
        assert len(data[name]) == exp[name]
    assert {"companies.json", "people.json", "deals.json", "tasks.json", "review.csv", "report.md"} <= \
        {p.name for p in out.iterdir()}


@needs_extract
def test_report_has_required_sections(dry_run):
    report = (dry_run[1] / "report.md").read_text(encoding="utf-8")
    for heading in ("## Records in → out", "## Normalised", "## Flagged for review", "## Skipped",
                    "## Orphans kept", "expected_counts"):
        assert heading in report
    assert "MISMATCH" not in report


@needs_extract
def test_every_merge_is_flagged_for_review(dry_run):
    people = dry_run[2]["people"]
    merged = [p for p in people if p["values"].get("merged_salesforce_ids")]
    assert len(merged) == 21
    assert all(any(f["reason"] == "duplicate_merged" for f in p["_migration"]["flags"]) for p in merged)


@needs_extract
def test_emails_are_unique_across_people(dry_run):
    emails = [e for p in dry_run[2]["people"] for e in p["values"].get("email_addresses", [])]
    assert len(emails) == len(set(emails))   # Attio rejects duplicates


@needs_extract
def test_orphans_are_kept_and_marked(dry_run):
    people, tasks = dry_run[2]["people"], dry_run[2]["tasks"]
    assert sum("no_account_in_salesforce" in p["_migration"]["orphan"] for p in people) == 12
    assert sum("no_contact" in t["_migration"]["orphan"] for t in tasks) == 6


@needs_extract
def test_contacts_without_a_role_keep_their_company(dry_run):
    # Regression: the 4 pre-existing household contacts have no Title and must keep their account link.
    people, companies = dry_run[2]["people"], dry_run[2]["companies"]
    households = {c["salesforce_id"] for c in companies if c["values"]["name"].endswith("Household")}
    linked = {p["references"]["company"][0]["salesforce_id"] for p in people if "company" in p["references"]}
    assert households <= linked
    assert not any(c["_migration"]["orphan"] for c in companies)


@needs_extract
def test_tenants_have_no_company_and_are_linked_to_deals(dry_run):
    people, deals = dry_run[2]["people"], dry_run[2]["deals"]
    tenants = [p for p in people if p["values"].get("crm_roles") == ["Tenant"]]
    assert tenants and not any("company" in p["references"] for p in tenants)
    on_deals = {r["salesforce_id"] for d in deals for r in d["references"].get("associated_people", [])}
    assert sum(p["salesforce_id"] in on_deals for p in tenants) >= 38


@needs_extract
def test_deal_values_are_normalised(dry_run):
    deals = dry_run[2]["deals"]
    assert {d["values"].get("property_state") for d in deals} <= {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"}
    assert all(d["values"].get("property_state") for d in deals)
    for d in deals:
        if "weekly_rent" in d["values"]:
            assert d["values"]["value"] == round(d["values"]["weekly_rent"] * 52, 2)
    assert sum("lease_start_date" not in d["values"] for d in deals) == 5
