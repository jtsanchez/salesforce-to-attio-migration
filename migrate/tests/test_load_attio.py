"""migrate/load_attio.py against a fake, in-memory Attio. No network: sockets are blocked in every test.

The fake follows the request/response shapes in the Attio REST docs, but it is a mock: passing these
tests does not prove the loader works against a live workspace (see README.md "Attio loader").
"""
import json
import socket
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib import error as urlerror
from urllib.parse import parse_qs, urlparse

import pytest

import load_attio as la

STAGES = ["Lead", "In Progress", "Won", "Lost"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("test attempted a real network connection")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(la, "urllib_transport", blocked)


# --- fake Attio ----------------------------------------------------------------------------------

class FakeAttio:
    """Routes (method, path) like the Attio v2 API and keeps records in memory."""

    def __init__(self, objects=("companies", "people", "deals"), stages=STAGES, workspace="ws-test",
                 auto_company_from_email=False):
        self.objects = list(objects)
        self.stages = list(stages)
        self.workspace = workspace
        self.auto_company = auto_company_from_email
        self.attributes = {o: {"name", "domains", "email_addresses", "stage", "value"} for o in self.objects}
        self.options = {}
        self.records = {o: {} for o in self.objects}       # record_id -> values (plain, as written)
        self.tasks = {}
        self.calls = []
        self.script = []                                    # queued (status, headers, body) to return first

    # helpers to inspect what was written
    def bodies(self):
        return [c["body"] for c in self.calls if c["body"] is not None]

    def __call__(self, method, url, headers, body, timeout=30):
        parsed = urlparse(url)
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "path": parsed.path, "query": parse_qs(parsed.query),
                           "body": payload, "headers": headers})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            status, hdrs, resp = item
            return status, hdrs, json.dumps(resp).encode()
        status, resp = self.route(method, parsed.path, parse_qs(parsed.query), payload)
        return status, {}, json.dumps(resp).encode()

    def route(self, method, path, query, body):
        parts = path.strip("/").split("/")[1:]   # drop "v2"
        if parts == ["self"]:
            return 200, {"active": True, "workspace_id": self.workspace}
        if parts == ["objects"]:
            return 200, {"data": [{"api_slug": o} for o in self.objects]}
        if parts[0] == "objects" and parts[1] not in self.objects:
            return 404, {"message": "object not found"}
        if parts[0] == "objects" and parts[2:] == ["attributes"]:
            if method == "GET":
                return 200, {"data": [{"api_slug": a} for a in sorted(self.attributes[parts[1]])]}
            self.attributes[parts[1]].add(body["data"]["api_slug"])
            return 200, {"data": body["data"]}
        if parts[0] == "objects" and parts[2] == "attributes" and parts[4:] == ["options"]:
            key = (parts[1], parts[3])
            if method == "GET":
                return 200, {"data": [{"title": t} for t in sorted(self.options.get(key, set()))]}
            self.options.setdefault(key, set()).add(body["data"]["title"])
            return 200, {"data": {"title": body["data"]["title"]}}
        if parts[0] == "objects" and parts[2:] == ["attributes", "stage", "statuses"]:
            return 200, {"data": [{"title": s} for s in self.stages]}
        if parts[0] == "objects" and parts[2:] == ["records"] and method == "PUT":
            return self.upsert(parts[1], query["matching_attribute"][0], body["data"]["values"])
        if parts[0] == "objects" and parts[2:] == ["records", "query"]:
            rows = list(self.records[parts[1]].items())[body["offset"]:body["offset"] + body["limit"]]
            return 200, {"data": [{"id": {"record_id": rid}, "values": attio_shape(v), "web_url": f"https://app/{rid}"}
                                  for rid, v in rows]}
        if parts == ["tasks"] and method == "POST":
            tid = str(uuid.uuid4())
            self.tasks[tid] = body["data"]
            return 200, {"data": {"id": {"task_id": tid}}}
        if parts[0] == "tasks" and method == "PATCH":
            self.tasks[parts[1]].update(body["data"])
            return 200, {"data": {"id": {"task_id": parts[1]}}}
        return 404, {"message": f"no route {method} {path}"}

    def upsert(self, obj, match, values):
        missing = [k for k in values if k not in self.attributes[obj] and k not in
                   ("associated_company", "associated_people", "company", "owner", "description", "name",
                    "phone_numbers", "primary_location")]
        if missing:
            return 400, {"message": f"unknown attributes {missing}"}
        matches = [rid for rid, v in self.records[obj].items() if v.get(match) == values.get(match)]
        if len(matches) > 1:
            return 400, {"message": "multiple matches"}
        rid = matches[0] if matches else str(uuid.uuid4())
        self.records[obj][rid] = values
        if obj == "people" and self.auto_company and not matches:
            for email in values.get("email_addresses", []):
                domain = email.split("@")[1]
                if not any(domain in c.get("domains", []) for c in self.records["companies"].values()):
                    self.records["companies"][str(uuid.uuid4())] = {"name": domain, "domains": [domain]}
        return 200, {"data": {"id": {"record_id": rid}, "values": attio_shape(values)}}


def attio_shape(values):
    """Plain written values -> Attio's read shape (arrays of {value}/{domain})."""
    out = {}
    for k, v in values.items():
        if k == "domains":
            out[k] = [{"domain": d} for d in v]
        else:
            out[k] = [{"value": v}]
    return out


# --- fixtures ------------------------------------------------------------------------------------

def company(sf, name, domains=None):
    v = {"salesforce_id": sf, "name": name}
    if domains:
        v["domains"] = domains
    return {"salesforce_id": sf, "values": v, "references": {}, "_migration": {"flags": ["x"]}}


def person(sf, email, company_sf=None):
    rec = {"salesforce_id": sf, "values": {"salesforce_id": sf, "name": {"full_name": sf},
                                           "email_addresses": [email]},
           "references": {}, "_migration": {"source_ids": [sf]}}
    if company_sf:
        rec["references"]["company"] = [{"target_object": "companies", "salesforce_id": company_sf}]
    return rec


def deal(sf, company_sf, people_sf, stage="Won"):
    return {"salesforce_id": sf, "values": {"salesforce_id": sf, "name": sf, "stage": stage, "weekly_rent": 500},
            "references": {"owner": [{"default_owner": True}],
                           "associated_company": [{"target_object": "companies", "salesforce_id": company_sf}],
                           "associated_people": [{"target_object": "people", "salesforce_id": p} for p in people_sf]},
            "_migration": {}}


def task(sf, deal_sf, person_sf=None, content="Routine inspection"):
    links = [{"target_object": "deals", "salesforce_id": deal_sf}]
    if person_sf:
        links.append({"target_object": "people", "salesforce_id": person_sf})
    return {"salesforce_id": sf, "values": {"content": content, "format": "plaintext",
                                            "deadline_at": "2025-06-25T09:00:00+10:00", "is_completed": False},
            "references": {"linked_records": links, "assignees": [{"default_owner": True}]}, "_migration": {}}


def write_transformed(directory, companies, people, deals, tasks):
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in (("companies", companies), ("people", people), ("deals", deals), ("tasks", tasks)):
        (directory / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")


@pytest.fixture
def workdir(tmp_path):
    """Small dataset on a real (non-reserved) agency domain, plus a .env with test credentials."""
    t = tmp_path / "transformed"
    write_transformed(
        t,
        [company("001A", "Harbourline Holdings", ["harbourline.com.au"]), company("001B", "Kiri Singh")],
        [person("003A", "kiri@harbourline.com.au", "001A"), person("003B", "tenant1@harbourline.com.au")],
        [deal("006A", "001A", ["003B"]), deal("006B", "001B", [])],
        [task("00TA", "006A", "003B"), task("00TB", "006B")],
    )
    (tmp_path / ".env").write_text("ATTIO_API_KEY=test-key-123\nATTIO_DEFAULT_OWNER_EMAIL=owner@agency.example\n",
                                   encoding="utf-8")
    return tmp_path


def run(workdir, fake, *extra, log=None):
    lines = []
    code = la.main(["--transformed", str(workdir / "transformed"), "--out", str(workdir / "load"),
                    "--env-file", str(workdir / ".env"), *extra],
                   transport=fake, sleep=lambda s: None, clock=lambda: 0.0, log=log or lines.append)
    return code, lines


# --- dry run -------------------------------------------------------------------------------------

def test_default_is_dry_run_and_makes_no_http_requests(workdir):
    fake = FakeAttio()
    code, lines = run(workdir, fake)
    assert code == la.EXIT_OK
    assert fake.calls == []
    plan = json.loads((workdir / "load" / "dry_run_plan.json").read_text(encoding="utf-8"))
    assert plan["would_upsert"] == {"companies": 2, "people": 2, "deals": 2}
    assert "DRY RUN" in "\n".join(lines)


def test_dry_run_needs_no_api_key(workdir):
    (workdir / ".env").unlink()
    fake = FakeAttio()
    assert run(workdir, fake, "--dry-run")[0] == la.EXIT_OK and fake.calls == []


def test_dry_run_reports_unresolvable_references(workdir):
    t = workdir / "transformed"
    deals = json.loads((t / "deals.json").read_text())
    deals[0]["references"]["associated_company"][0]["salesforce_id"] = "001-missing"
    (t / "deals.json").write_text(json.dumps(deals))
    code, _ = run(workdir, FakeAttio())
    assert code == la.EXIT_FAILURES
    plan = json.loads((workdir / "load" / "dry_run_plan.json").read_text(encoding="utf-8"))
    assert plan["reference_problems"] == ["deals 006A: associated_company -> companies 001-missing"]
    assert "unresolved" in plan["sample_requests"]["deals"]["body"]
    assert run(workdir, FakeAttio(), "--apply")[0] == la.EXIT_BLOCKED   # apply refuses to start


def test_dry_run_and_apply_are_mutually_exclusive(workdir):
    with pytest.raises(SystemExit):
        run(workdir, FakeAttio(), "--dry-run", "--apply")


# --- apply ----------------------------------------------------------------------------------------

def test_apply_loads_everything_in_order_with_resolved_references(workdir):
    fake = FakeAttio()
    code, lines = run(workdir, fake, "--apply")
    assert code == la.EXIT_OK, lines
    assert len(fake.records["companies"]) == 2 and len(fake.records["people"]) == 2
    assert len(fake.records["deals"]) == 2 and len(fake.tasks) == 2

    puts = [c for c in fake.calls if c["method"] == "PUT"]
    assert [c["path"].split("/")[3] for c in puts] == ["companies"] * 2 + ["people"] * 2 + ["deals"] * 2
    assert all(c["query"] == {"matching_attribute": ["salesforce_id"]} for c in puts)

    company_ids = {v["salesforce_id"]: rid for rid, v in fake.records["companies"].items()}
    kiri = next(v for v in fake.records["people"].values() if v["salesforce_id"] == "003A")
    assert kiri["company"] == [{"target_object": "companies", "target_record_id": company_ids["001A"]}]
    d = next(v for v in fake.records["deals"].values() if v["salesforce_id"] == "006A")
    assert d["owner"] == ["owner@agency.example"]
    assert d["associated_company"][0]["target_record_id"] == company_ids["001A"]
    t = next(iter(fake.tasks.values()))
    assert t["assignees"] == [{"workspace_member_email_address": "owner@agency.example"}]
    assert {lr["target_object"] for lr in t["linked_records"]} <= {"deals", "people"}


def test_apply_never_sends_migration_metadata_or_salesforce_refs(workdir):
    fake = FakeAttio()
    run(workdir, fake, "--apply")
    assert "_migration" not in json.dumps(fake.bodies())
    for body in fake.bodies():
        for v in (body.get("data") or {}).get("values", {}).values():
            for item in v if isinstance(v, list) else []:
                if isinstance(item, dict) and "target_object" in item:
                    assert "target_record_id" in item and "salesforce_id" not in item


def test_apply_sends_bearer_key_and_never_logs_it(workdir):
    fake = FakeAttio()
    code, lines = run(workdir, fake, "--apply")
    assert all(c["headers"]["Authorization"] == "Bearer test-key-123" for c in fake.calls)
    assert "test-key-123" not in "\n".join(lines)
    assert "test-key-123" not in (workdir / "load" / "last_run.json").read_text()


def test_apply_creates_missing_custom_attributes_and_options_once(workdir):
    fake = FakeAttio()
    run(workdir, fake, "--apply")
    created = [c for c in fake.calls if c["method"] == "POST" and c["path"].endswith("/attributes")]
    slugs = {(c["path"].split("/")[3], c["body"]["data"]["api_slug"]) for c in created}
    assert ("deals", "weekly_rent") in slugs and ("people", "salesforce_id") in slugs
    weekly = next(c for c in created if c["body"]["data"]["api_slug"] == "weekly_rent")["body"]["data"]
    assert weekly["type"] == "currency" and weekly["config"] == {
        "currency": {"default_currency_code": "AUD", "display_type": "symbol"}}
    assert ("deals", "property_state") in fake.options and "NSW" in fake.options[("deals", "property_state")]

    fake.calls.clear()
    run(workdir, fake, "--apply")
    assert not [c for c in fake.calls if c["method"] == "POST" and "/attributes" in c["path"]]


def test_rerun_is_idempotent_and_updates_tasks_instead_of_duplicating(workdir):
    fake = FakeAttio()
    run(workdir, fake, "--apply")
    fake.calls.clear()
    code, _ = run(workdir, fake, "--apply")
    assert code == la.EXIT_OK
    assert len(fake.records["people"]) == 2 and len(fake.tasks) == 2
    task_calls = [c for c in fake.calls if "/tasks" in c["path"]]
    assert {c["method"] for c in task_calls} == {"PATCH"}
    assert all("content" not in c["body"]["data"] for c in task_calls)   # content can only be set on create


def test_batches_checkpoint_state_after_each_batch(workdir):
    fake = FakeAttio()
    saves = []
    original = la.Loader.save_state
    la.Loader.save_state = lambda self: (saves.append(json.loads(json.dumps(self.state["ids"]))), original(self))
    try:
        run(workdir, fake, "--apply", "--batch-size", "1")
    finally:
        la.Loader.save_state = original
    assert any(len(s["companies"]) == 1 for s in saves)       # saved mid-way through companies
    state = json.loads((workdir / "load" / "state.json").read_text())
    assert state["workspace_id"] == "ws-test" and len(state["ids"]["tasks"]) == 2


def test_failed_record_is_reported_and_others_continue(workdir):
    fake = FakeAttio()
    original = fake.upsert

    def flaky(obj, match, values):
        if values.get("salesforce_id") == "003A":
            return 400, {"message": "invalid email"}
        return original(obj, match, values)
    fake.upsert = flaky
    code, _ = run(workdir, fake, "--apply")
    assert code == la.EXIT_FAILURES
    summary = json.loads((workdir / "load" / "last_run.json").read_text())
    assert [f["salesforce_id"] for f in summary["failures"]] == ["003A"]
    assert summary["results"]["deals"]["upserted"] == 2 and summary["results"]["tasks"]["created"] == 2


def test_reference_to_a_failed_record_fails_only_the_dependent_record(workdir):
    fake = FakeAttio()
    original = fake.upsert
    fake.upsert = lambda o, m, v: (400, {"message": "nope"}) if v.get("salesforce_id") == "003B" else original(o, m, v)
    run(workdir, fake, "--apply")
    failures = json.loads((workdir / "load" / "last_run.json").read_text())["failures"]
    failed = {f["salesforce_id"] for f in failures}
    assert failed == {"003B", "006A", "00TA"}           # 006A and 00TA reference person 003B
    assert any("no Attio id yet" in f["error"] for f in failures)


# --- pre-flight -------------------------------------------------------------------------------------

def test_apply_without_credentials_is_blocked_before_any_request(workdir):
    (workdir / ".env").write_text("ATTIO_DEFAULT_OWNER_EMAIL=owner@agency.example\n")
    fake = FakeAttio()
    code, lines = run(workdir, fake, "--apply")
    assert code == la.EXIT_BLOCKED and fake.calls == []
    assert "ATTIO_API_KEY" in "\n".join(lines)


def test_deals_object_disabled_blocks_before_writes(workdir):
    fake = FakeAttio(objects=("companies", "people"))
    code, lines = run(workdir, fake, "--apply")
    assert code == la.EXIT_BLOCKED and "Settings > Objects" in "\n".join(lines)
    assert not [c for c in fake.calls if c["method"] in ("PUT", "POST", "PATCH")]


def test_missing_deal_stage_blocks(workdir):
    fake = FakeAttio(stages=["Lead", "In Progress", "Lost"])
    code, lines = run(workdir, fake, "--apply")
    assert code == la.EXIT_BLOCKED and "Won" in "\n".join(lines)
    assert not [c for c in fake.calls if c["method"] == "PUT"]


def test_state_from_another_workspace_blocks(workdir):
    run(workdir, FakeAttio(workspace="ws-sandbox"), "--apply")
    fake = FakeAttio(workspace="ws-production")
    code, lines = run(workdir, fake, "--apply")
    assert code == la.EXIT_BLOCKED and "ws-sandbox" in "\n".join(lines)
    assert not [c for c in fake.calls if c["method"] in ("PUT", "PATCH")]


# --- auto-company guard -------------------------------------------------------------------------------

@pytest.fixture
def example_domain_workdir(workdir):
    t = workdir / "transformed"
    write_transformed(
        t,
        [company("001A", "Harbourline Holdings", ["harbourline.example.com"])],
        [person("003A", "kiri.singh@example.com", "001A"), person("003B", "amelia.tran@example.net")],
        [deal("006A", "001A", ["003B"])],
        [],
    )
    return workdir


def test_guard_detects_reserved_and_shared_domains():
    data = {"companies": [company("1", "A", ["agency.com.au"])],
            "people": [person("p1", "a@example.com"), person("p2", "b@sub.example.org"), person("p3", "c@x.test"),
                       person("p4", "d@gmail.com"), person("p5", "e@gmail.com"), person("p6", "f@gmail.com"),
                       person("p7", "g@agency.com.au"), person("p8", "h@agency.com.au"), person("p9", "i@agency.com.au")]}
    g = la.auto_company_guard(data)
    assert set(g["reserved_domains"]) == {"example.com", "sub.example.org", "x.test"}
    assert g["shared_non_company_domains"] == {"gmail.com": 3}           # agency.com.au is one of our companies
    assert g["people_affected"] == 6


def test_guard_blocks_apply_on_example_domains_without_flag(example_domain_workdir):
    fake = FakeAttio()
    code, lines = run(example_domain_workdir, fake, "--apply")
    assert code == la.EXIT_BLOCKED and fake.calls == []
    assert "auto-company guard" in "\n".join(lines)


def test_guard_flag_allows_apply_and_audit_reports_auto_created_companies(example_domain_workdir):
    fake = FakeAttio(auto_company_from_email=True)   # simulates Attio creating companies from email domains
    code, lines = run(example_domain_workdir, fake, "--apply", "--accept-auto-company-risk")
    assert code == la.EXIT_OK
    audit = json.loads((example_domain_workdir / "load" / "auto_companies.json").read_text())
    assert sorted(d for a in audit for d in a["domains"]) == ["example.com", "example.net"]
    assert "WARNING" in "\n".join(lines)
    assert len(fake.records["companies"]) == 3     # nothing deleted automatically


def test_audit_ignores_our_own_companies(example_domain_workdir):
    fake = FakeAttio(auto_company_from_email=False)
    run(example_domain_workdir, fake, "--apply", "--accept-auto-company-risk")
    audit = json.loads((example_domain_workdir / "load" / "auto_companies.json").read_text())
    assert audit == []


def test_audit_paginates_company_listing(example_domain_workdir, monkeypatch):
    fake = FakeAttio(auto_company_from_email=True)
    for i in range(1200):
        fake.records["companies"][f"pre-{i}"] = {"name": f"c{i}", "domains": [f"c{i}.com.au"]}
    run(example_domain_workdir, fake, "--apply", "--accept-auto-company-risk")
    offsets = [c["body"]["offset"] for c in fake.calls if c["path"].endswith("/records/query")]
    assert offsets == [0, 500, 1000]


# --- retries and pacing ---------------------------------------------------------------------------------

def client_with(fake, sleeps, now=None, **kw):
    """Client whose clock advances when it sleeps, so pacing never adds sleeps between retries."""
    t = [0.0]

    def sleep(s):
        sleeps.append(s)
        t[0] += s
    return la.AttioClient("k", transport=fake, sleep=sleep, clock=lambda: t[0],
                          now=now or (lambda: datetime(2026, 11, 18, 0, 0, 0, tzinfo=timezone.utc)),
                          rng=lambda: 1.0, **kw)


def test_429_waits_until_retry_after_date_then_succeeds():
    fake = FakeAttio()
    reset = datetime(2026, 11, 18, 0, 0, 2, tzinfo=timezone.utc)
    fake.script = [(429, {"retry-after": format_datetime(reset, usegmt=True)}, {"message": "rate limited"})]
    sleeps = []
    resp = client_with(fake, sleeps).request("GET", "/v2/self")
    assert resp["workspace_id"] == "ws-test"
    assert 2.0 in sleeps and len(fake.calls) == 2


def test_429_with_numeric_retry_after_and_missing_header():
    fake = FakeAttio()
    fake.script = [(429, {"retry-after": "3"}, {}), (429, {}, {})]
    sleeps = []
    client_with(fake, sleeps).request("GET", "/v2/self")
    assert sleeps[:2] == [3.0, 1.0]


def test_5xx_and_connection_errors_back_off_exponentially_then_succeed():
    fake = FakeAttio()
    fake.script = [(503, {}, {}), urlerror.URLError("reset"), (502, {}, {})]
    sleeps = []
    client = client_with(fake, sleeps)
    assert client.request("GET", "/v2/self")["workspace_id"] == "ws-test"
    assert sleeps == [1.0, 2.0, 4.0]          # rng pinned to 1.0 -> no jitter reduction
    assert client.stats["retried_5xx"] == 2 and client.stats["retried_connection"] == 1


def test_4xx_is_not_retried():
    fake = FakeAttio()
    fake.script = [(400, {}, {"message": "bad value"})]
    sleeps = []
    with pytest.raises(la.AttioError) as e:
        client_with(fake, sleeps).request("PUT", "/v2/objects/people/records", {"data": {}})
    assert e.value.status == 400 and len(fake.calls) == 1 and sleeps == []


def test_retries_are_bounded():
    fake = FakeAttio()
    fake.script = [(500, {}, {})] * 10
    with pytest.raises(la.AttioError, match="gave up after 3 attempts"):
        client_with(fake, [], max_retries=2).request("GET", "/v2/self")
    assert len(fake.calls) == 3


def test_backoff_is_capped_and_jittered():
    c = la.AttioClient("k", transport=FakeAttio(), rng=lambda: 0.0)
    assert c._backoff(0) == 0.5 and c._backoff(10) == 15.0     # cap 30s, jitter down to half


def test_writes_are_paced_under_attio_limit():
    fake = FakeAttio()
    t = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s
    client = la.AttioClient("k", transport=fake, sleep=sleep, clock=lambda: t[0], writes_per_second=20)
    for _ in range(5):
        client.request("POST", "/v2/tasks", {"data": {"content": "x"}})
    assert len(sleeps) == 4 and all(abs(s - 0.05) < 1e-9 for s in sleeps)   # 20/s, below Attio's 25/s


def test_reads_and_writes_are_paced_separately():
    limiter_sleeps = []
    lim = la.RateLimiter({"write": 20, "read": 80}, clock=lambda: 0.0, sleep=limiter_sleeps.append)
    lim.wait("write")
    lim.wait("read")
    assert limiter_sleeps == []


# --- config ---------------------------------------------------------------------------------------------

def test_env_file_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nATTIO_API_KEY="abc"\nATTIO_DEFAULT_OWNER_EMAIL = owner@x.example\nBAD LINE\n')
    assert la.read_env_file(p) == {"ATTIO_API_KEY": "abc", "ATTIO_DEFAULT_OWNER_EMAIL": "owner@x.example"}
    assert la.read_env_file(tmp_path / "missing") == {}


def test_real_transformed_data_dry_run(tmp_path):
    """The real data/transformed output passes the dry run with no reference problems."""
    from pathlib import Path
    real = Path(__file__).resolve().parents[2] / "data" / "transformed"
    if not (real / "people.json").exists():
        pytest.skip("data/transformed not present; run migrate/transform.py first")
    lines = []
    code = la.main(["--transformed", str(real), "--out", str(tmp_path)], transport=FakeAttio(), log=lines.append)
    plan = json.loads((tmp_path / "dry_run_plan.json").read_text(encoding="utf-8"))
    assert code == la.EXIT_OK and plan["reference_problems"] == []
    assert plan["auto_company_guard"]["risky_domains"] == ["example.com", "example.net", "example.org"]
