"""Load data/transformed/ into Attio through the REST API (v2).

DEFAULT IS A DRY RUN. Without --apply this makes no HTTP requests at all and needs no API key:
it checks the transformed files, resolves every reference offline, runs the auto-company
guard and writes the planned requests to data/load/dry_run_plan.json.

With --apply (needs ATTIO_API_KEY and ATTIO_DEFAULT_OWNER_EMAIL in .env or the environment):
  1. pre-flight: identify the workspace (GET /v2/self), check the companies/people/deals objects
     exist (Deals is off by default), check the deal stages exist, and create missing custom
     attributes and select options from mapping.yaml `attio_setup`
  2. upsert companies -> people -> deals with PUT /v2/objects/{object}/records?matching_attribute=salesforce_id
  3. create tasks (POST /v2/tasks), or update ones loaded before (PATCH /v2/tasks/{id})
  4. audit: list companies Attio holds that have no salesforce_id but use one of our people's risky
     email domains (Attio auto-creates/matches companies from email domains), and write them to
     data/load/auto_companies.json for a human to review. Nothing is deleted automatically.

Batching and retries: Attio has no bulk record endpoint, so records go one request each, in batches
of --batch-size, with data/load/state.json (salesforce_id -> Attio id) saved after every batch so a
rerun resumes. Writes are paced under Attio's documented 25/s (reads under 100/s). 429 responses wait
until the Retry-After date; 5xx, 408 and connection errors back off exponentially with jitter; other
4xx errors are not retried and are recorded as failures for that record.

TESTED AGAINST MOCKED HTTP ONLY (migrate/tests/test_load_attio.py). Not yet run against a live
workspace; see README.md "Attio loader".

Usage:
  .venv/Scripts/python -I migrate/load_attio.py                  # dry run (default)
  .venv/Scripts/python -I migrate/load_attio.py --apply [--accept-auto-company-risk] [--batch-size 50]
"""
import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlencode

import yaml

ROOT = Path(__file__).resolve().parent.parent
MAPPING = Path(__file__).resolve().parent / "mapping.yaml"
BASE_URL = "https://api.attio.com"
OBJECT_ORDER = ("companies", "people", "deals")
MATCH_ATTRIBUTE = "salesforce_id"

# RFC 2606 / RFC 6761 reserved names: never real mailboxes, so a company "matched" from them is junk.
RESERVED_DOMAINS = {"example.com", "example.net", "example.org"}
RESERVED_SUFFIXES = (".example", ".test", ".invalid", ".localhost")
SHARED_DOMAIN_MIN_PEOPLE = 3   # a non-company domain used by this many people is treated as risky too

EXIT_OK, EXIT_FAILURES, EXIT_BLOCKED = 0, 1, 2


class AttioError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status


class LoadBlocked(Exception):
    """Pre-flight refused to load. Nothing was written."""


class UnresolvedReference(Exception):
    pass


# --- HTTP ------------------------------------------------------------------------------------

def urllib_transport(method, url, headers, body, timeout=30):
    """Real HTTP via the standard library. Returns (status, lower-cased headers, body bytes)."""
    req = urlrequest.Request(url, data=body, method=method, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urlerror.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


class RateLimiter:
    """Minimum spacing between requests of each kind (simple and deterministic to test)."""

    def __init__(self, per_second, clock, sleep):
        self.interval = {k: 1.0 / v for k, v in per_second.items()}
        self.last = {}
        self.clock, self.sleep = clock, sleep

    def wait(self, kind):
        now = self.clock()
        due = self.last.get(kind, float("-inf")) + self.interval[kind]
        if now < due:
            self.sleep(due - now)
            now = due
        self.last[kind] = now


class AttioClient:
    def __init__(self, api_key, transport=urllib_transport, sleep=time.sleep, clock=time.monotonic,
                 now=lambda: datetime.now(timezone.utc), rng=random.random, max_retries=5,
                 writes_per_second=20, reads_per_second=80, base_url=BASE_URL):
        self.api_key = api_key
        self.transport, self.sleep, self.now, self.rng = transport, sleep, now, rng
        self.max_retries = max_retries
        self.base_url = base_url
        self.limiter = RateLimiter({"write": writes_per_second, "read": reads_per_second}, clock, sleep)
        self.stats = Counter()

    def _retry_after(self, headers):
        value = headers.get("retry-after")
        if not value:
            return 1.0
        try:
            return min(max(float(value), 0.1), 60.0)
        except ValueError:
            pass
        try:
            delta = (parsedate_to_datetime(value) - self.now()).total_seconds()
        except (TypeError, ValueError):
            return 1.0
        return min(max(delta, 0.1), 60.0)

    def _backoff(self, attempt):
        return min(30.0, 2 ** attempt) * (0.5 + self.rng() / 2)

    def request(self, method, path, body=None, query=None):
        kind = "read" if method == "GET" or path.endswith("/records/query") else "write"
        url = self.base_url + path + (f"?{urlencode(query)}" if query else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last_error = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait(kind)
            self.stats["requests"] += 1
            try:
                status, resp_headers, raw = self.transport(method, url, headers, data)
            except (urlerror.URLError, TimeoutError, ConnectionError) as e:
                last_error = AttioError(None, f"connection error: {e}")
                self.stats["retried_connection"] += 1
                self.sleep(self._backoff(attempt))
                continue
            if 200 <= status < 300:
                return json.loads(raw) if raw else {}
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            last_error = AttioError(status, text[:500])
            if status == 429:
                self.stats["retried_429"] += 1
                self.sleep(self._retry_after(resp_headers))
                continue
            if status >= 500 or status == 408:
                self.stats["retried_5xx"] += 1
                self.sleep(self._backoff(attempt))
                continue
            raise last_error  # other 4xx: retrying won't help
        raise AttioError(last_error.status, f"gave up after {self.max_retries + 1} attempts: {last_error}")


# --- configuration -----------------------------------------------------------------------------

def read_env_file(path):
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes. Never logs values."""
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def setting(name, env_file):
    return os.environ.get(name) or env_file.get(name) or None


# --- offline preparation -----------------------------------------------------------------------

def load_transformed(directory):
    data = {}
    for name in ("companies", "people", "deals", "tasks"):
        path = directory / f"{name}.json"
        if not path.exists():
            raise LoadBlocked(f"{path} not found; run migrate/transform.py first")
        data[name] = json.loads(path.read_text(encoding="utf-8"))
    return data


def email_domain(email):
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def is_reserved_domain(domain):
    return domain in RESERVED_DOMAINS or domain.endswith(RESERVED_SUFFIXES) or \
        any(domain.endswith("." + d) for d in RESERVED_DOMAINS)


def auto_company_guard(data):
    """Find email domains that could make Attio generate junk companies.

    Attio documents that creating a person generates or matches a company from the person's email
    domain. Risky domains: reserved test domains, and domains shared by several people that match
    none of the companies we load (e.g. a free-mail or agency domain).
    """
    company_domains = {d for c in data["companies"] for d in c["values"].get("domains", [])}
    by_domain = Counter(email_domain(e) for p in data["people"] for e in p["values"].get("email_addresses", []))
    reserved = {d: n for d, n in by_domain.items() if is_reserved_domain(d)}
    shared = {d: n for d, n in by_domain.items() if d not in reserved and d not in company_domains
              and n >= SHARED_DOMAIN_MIN_PEOPLE}
    risky = set(reserved) | set(shared)
    people_affected = sum(1 for p in data["people"]
                          if any(email_domain(e) in risky for e in p["values"].get("email_addresses", [])))
    return {"reserved_domains": reserved, "shared_non_company_domains": shared,
            "risky_domains": sorted(risky), "people_affected": people_affected}


def check_references(data):
    """Every reference must point at a record in the transformed set. Returns a list of problems."""
    known = {name: {r["salesforce_id"] for r in data[name]} for name in OBJECT_ORDER}
    problems = []
    for name in ("people", "deals", "tasks"):
        for rec in data[name]:
            for attr, refs in rec.get("references", {}).items():
                for ref in refs:
                    if ref.get("default_owner"):
                        continue
                    target = ref.get("target_object")
                    if target not in known or ref.get("salesforce_id") not in known[target]:
                        problems.append(f"{name} {rec['salesforce_id']}: {attr} -> {target} {ref.get('salesforce_id')}")
    return problems


def resolve_reference(ref, ids, owner_email, attr):
    if ref.get("default_owner"):
        if attr == "assignees":
            return {"workspace_member_email_address": owner_email}
        return owner_email
    target, sf_id = ref["target_object"], ref["salesforce_id"]
    record_id = ids.get(target, {}).get(sf_id)
    if not record_id:
        raise UnresolvedReference(f"{attr}: {target} {sf_id} has no Attio id yet")
    return {"target_object": target, "target_record_id": record_id}


def record_payload(rec, ids, owner_email):
    """Attio `values` for one record: transformed values + resolved references. Never `_migration`."""
    values = dict(rec["values"])
    for attr, refs in rec.get("references", {}).items():
        values[attr] = [resolve_reference(r, ids, owner_email, attr) for r in refs]
    return {"data": {"values": values}}


def task_payload(rec, ids, owner_email, update=False):
    v = rec["values"]
    refs = rec["references"]
    data = {
        "deadline_at": v.get("deadline_at"),
        "is_completed": v.get("is_completed", False),
        "linked_records": [resolve_reference(r, ids, owner_email, "linked_records") for r in refs["linked_records"]],
        "assignees": [resolve_reference(r, ids, owner_email, "assignees") for r in refs.get("assignees", [])],
    }
    if not update:  # content and format can only be set on create
        data = {"content": v["content"], "format": v.get("format", "plaintext"), **data}
    return {"data": data}


# --- loader ------------------------------------------------------------------------------------

class Loader:
    def __init__(self, client, mapping, data, owner_email, state_path, batch_size=50, log=print):
        self.client, self.mapping, self.data = client, mapping, data
        self.owner_email, self.state_path, self.batch_size, self.log = owner_email, state_path, batch_size, log
        self.state = {"workspace_id": None, "ids": {n: {} for n in (*OBJECT_ORDER, "tasks")}}
        if state_path.exists():
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        self.results = {n: Counter() for n in (*OBJECT_ORDER, "tasks")}
        self.failures = []

    def save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # pre-flight -------------------------------------------------------------------------------
    def preflight(self):
        me = self.client.request("GET", "/v2/self")
        workspace = me.get("workspace_id") or (me.get("data") or {}).get("workspace_id")
        if not workspace:
            raise LoadBlocked("could not identify the workspace for this API key (GET /v2/self)")
        if self.state.get("workspace_id") not in (None, workspace):
            raise LoadBlocked(f"{self.state_path} belongs to workspace {self.state['workspace_id']}, but the key is "
                              f"for {workspace}. Use a different --state file for another workspace.")
        self.state["workspace_id"] = workspace

        objects = {o.get("api_slug") for o in self.client.request("GET", "/v2/objects").get("data", [])}
        missing = [o for o in OBJECT_ORDER if o not in objects]
        if missing:
            raise LoadBlocked(f"objects not available in the workspace: {missing}. Deals is disabled by "
                              f"default: enable it in Settings > Objects.")

        for obj, specs in self.mapping["attio_setup"]["custom_attributes"].items():
            existing = {a.get("api_slug") for a in
                        self.client.request("GET", f"/v2/objects/{obj}/attributes").get("data", [])}
            for spec in specs:
                if spec["api_slug"] not in existing:
                    self.client.request("POST", f"/v2/objects/{obj}/attributes", attribute_body(spec))
                    self.log(f"  created attribute {obj}.{spec['api_slug']}")
                for option in spec.get("options", []):
                    self.ensure_option(obj, spec["api_slug"], option)

        statuses = {s.get("title") for s in self.client.request(
            "GET", "/v2/objects/deals/attributes/stage/statuses").get("data", [])}
        needed = {d["values"]["stage"] for d in self.data["deals"] if d["values"].get("stage")}
        if needed - statuses:
            raise LoadBlocked(f"deal stages missing in Attio: {sorted(needed - statuses)}")
        self.save_state()

    def ensure_option(self, obj, attr, title):
        key = f"_options_{obj}_{attr}"
        if key not in self.__dict__:
            self.__dict__[key] = {o.get("title") for o in self.client.request(
                "GET", f"/v2/objects/{obj}/attributes/{attr}/options").get("data", [])}
        if title not in self.__dict__[key]:
            self.client.request("POST", f"/v2/objects/{obj}/attributes/{attr}/options", {"data": {"title": title}})
            self.__dict__[key].add(title)

    # records -------------------------------------------------------------------------------------
    def batches(self, rows):
        for i in range(0, len(rows), self.batch_size):
            yield rows[i:i + self.batch_size]

    def load_object(self, obj):
        ids = self.state["ids"]
        for batch in self.batches(self.data[obj]):
            for rec in batch:
                sf_id = rec["salesforce_id"]
                try:
                    payload = record_payload(rec, ids, self.owner_email)
                    resp = self.client.request("PUT", f"/v2/objects/{obj}/records", payload,
                                               query={"matching_attribute": MATCH_ATTRIBUTE})
                    ids[obj][sf_id] = resp["data"]["id"]["record_id"]
                    self.results[obj]["upserted"] += 1
                except (AttioError, UnresolvedReference, KeyError) as e:
                    self.fail(obj, sf_id, e)
            self.save_state()
            self.log(f"  {obj}: {sum(self.results[obj].values())}/{len(self.data[obj])}")

    def load_tasks(self):
        ids = self.state["ids"]
        for batch in self.batches(self.data["tasks"]):
            for rec in batch:
                sf_id = rec["salesforce_id"]
                existing = ids["tasks"].get(sf_id)
                try:
                    if existing:
                        self.client.request("PATCH", f"/v2/tasks/{existing}",
                                            task_payload(rec, ids, self.owner_email, update=True))
                        self.results["tasks"]["updated"] += 1
                    else:
                        resp = self.client.request("POST", "/v2/tasks", task_payload(rec, ids, self.owner_email))
                        ids["tasks"][sf_id] = resp["data"]["id"]["task_id"]
                        self.results["tasks"]["created"] += 1
                except (AttioError, UnresolvedReference, KeyError) as e:
                    self.fail("tasks", sf_id, e)
            self.save_state()
            self.log(f"  tasks: {sum(self.results['tasks'].values())}/{len(self.data['tasks'])}")

    def fail(self, obj, sf_id, error):
        self.results[obj]["failed"] += 1
        self.failures.append({"object": obj, "salesforce_id": sf_id, "error": str(error)})

    # audit -----------------------------------------------------------------------------------------
    def audit_auto_companies(self, risky_domains):
        """Companies with no salesforce_id whose domain is one of our risky email domains."""
        found, offset, page = [], 0, 500
        while True:
            resp = self.client.request("POST", "/v2/objects/companies/records/query",
                                       {"limit": page, "offset": offset})
            rows = resp.get("data", [])
            for r in rows:
                values = r.get("values", {})
                has_sf_id = any(v.get("value") for v in values.get(MATCH_ATTRIBUTE, []))
                domains = {(v.get("domain") or "").lower() for v in values.get("domains", [])}
                if not has_sf_id and domains & set(risky_domains):
                    found.append({"record_id": r.get("id", {}).get("record_id"), "domains": sorted(domains),
                                  "web_url": r.get("web_url")})
            if len(rows) < page:
                return found
            offset += page

    def run(self, risky_domains):
        self.preflight()
        for obj in OBJECT_ORDER:
            self.load_object(obj)
        self.load_tasks()
        return self.audit_auto_companies(risky_domains)


def attribute_body(spec):
    config = {}
    if spec["type"] == "currency":
        config = {"currency": spec.get("config", {})}
    elif spec["type"] == "record-reference":
        config = {"record_reference": spec.get("config", {})}
    return {"data": {
        "title": spec["title"],
        "description": spec.get("description", f"Migrated from Salesforce ({spec['api_slug']})"),
        "api_slug": spec["api_slug"],
        "type": spec["type"],
        "is_required": False,
        "is_unique": bool(spec.get("is_unique", False)),
        "is_multiselect": bool(spec.get("is_multiselect", False)),
        "config": config,
    }}


# --- entry point ---------------------------------------------------------------------------------

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def dry_run(data, mapping, guard, out_dir, log):
    problems = check_references(data)
    placeholder = {name: {r["salesforce_id"]: f"<attio-id:{name}:{r['salesforce_id']}>" for r in data[name]}
                   for name in OBJECT_ORDER}
    owner = "<ATTIO_DEFAULT_OWNER_EMAIL>"

    def sample(build):
        try:
            return build()
        except UnresolvedReference as e:   # reported in reference_problems; a dry run must not crash
            return {"unresolved": str(e)}

    samples = {}
    for name in OBJECT_ORDER:
        if data[name]:
            samples[name] = {"method": "PUT", "path": f"/v2/objects/{name}/records",
                             "query": {"matching_attribute": MATCH_ATTRIBUTE},
                             "body": sample(lambda: record_payload(data[name][0], placeholder, owner))}
    if data["tasks"]:
        samples["tasks"] = {"method": "POST", "path": "/v2/tasks",
                            "body": sample(lambda: task_payload(data["tasks"][0], placeholder, owner))}
    plan = {
        "mode": "dry-run (no HTTP requests were made)",
        "would_upsert": {n: len(data[n]) for n in OBJECT_ORDER},
        "would_create_or_update_tasks": len(data["tasks"]),
        "custom_attributes_checked": {o: [a["api_slug"] for a in specs]
                                      for o, specs in mapping["attio_setup"]["custom_attributes"].items()},
        "reference_problems": problems,
        "auto_company_guard": guard,
        "sample_requests": samples,
    }
    write_json(out_dir / "dry_run_plan.json", plan)
    log("DRY RUN: no requests sent to Attio.")
    log(f"  would upsert {plan['would_upsert']} and {plan['would_create_or_update_tasks']} tasks")
    log(f"  reference problems: {len(problems)}")
    return plan


def main(argv=None, transport=urllib_transport, sleep=time.sleep, clock=time.monotonic, log=print):
    ap = argparse.ArgumentParser(description="Load data/transformed into Attio (dry run unless --apply).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default: plan only, no HTTP requests")
    mode.add_argument("--apply", action="store_true", help="write to the Attio workspace for ATTIO_API_KEY")
    ap.add_argument("--accept-auto-company-risk", action="store_true",
                    help="allow --apply even though people use reserved/shared email domains")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--transformed", type=Path, default=ROOT / "data" / "transformed")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "load")
    ap.add_argument("--state", type=Path, default=None, help="default: <out>/state.json")
    ap.add_argument("--mapping", type=Path, default=MAPPING)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = ap.parse_args(argv)
    if args.batch_size < 1:
        ap.error("--batch-size must be at least 1")

    try:
        data = load_transformed(args.transformed)
    except LoadBlocked as e:
        log(f"BLOCKED: {e}")
        return EXIT_BLOCKED
    mapping = yaml.safe_load(args.mapping.read_text(encoding="utf-8"))
    guard = auto_company_guard(data)
    if guard["risky_domains"]:
        log(f"Auto-company guard: {guard['people_affected']} people use risky email domains "
            f"{guard['risky_domains']} (reserved: {sorted(guard['reserved_domains'])}). Attio may generate "
            f"companies from these domains when the people are created.")

    if not args.apply:
        plan = dry_run(data, mapping, guard, args.out, log)
        return EXIT_FAILURES if plan["reference_problems"] else EXIT_OK

    # --- apply ---
    env_file = read_env_file(args.env_file)
    api_key, owner = setting("ATTIO_API_KEY", env_file), setting("ATTIO_DEFAULT_OWNER_EMAIL", env_file)
    missing = [n for n, v in (("ATTIO_API_KEY", api_key), ("ATTIO_DEFAULT_OWNER_EMAIL", owner)) if not v]
    if missing:
        log(f"BLOCKED: {', '.join(missing)} not set (put them in .env, which is gitignored).")
        return EXIT_BLOCKED
    problems = check_references(data)
    if problems:
        log(f"BLOCKED: {len(problems)} unresolvable references, e.g. {problems[0]}. Rerun the transform.")
        return EXIT_BLOCKED
    if guard["risky_domains"] and not args.accept_auto_company_risk:
        log("BLOCKED by the auto-company guard. Load into a sandbox workspace first, then rerun with "
            "--accept-auto-company-risk and review data/load/auto_companies.json afterwards.")
        return EXIT_BLOCKED

    client = AttioClient(api_key, transport=transport, sleep=sleep, clock=clock)
    loader = Loader(client, mapping, data, owner, args.state or args.out / "state.json", args.batch_size, log)
    try:
        auto_companies = loader.run(guard["risky_domains"])
    except LoadBlocked as e:
        log(f"BLOCKED: {e}")
        return EXIT_BLOCKED

    summary = {"finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "workspace_id": loader.state["workspace_id"],
               "results": {k: dict(v) for k, v in loader.results.items()},
               "failures": loader.failures, "http": dict(client.stats),
               "auto_companies_found": len(auto_companies)}
    write_json(args.out / "last_run.json", summary)
    write_json(args.out / "auto_companies.json", auto_companies)
    log(f"APPLIED: {summary['results']}; failures {len(loader.failures)}; http {summary['http']}")
    if auto_companies:
        log(f"WARNING: {len(auto_companies)} companies without a salesforce_id use risky email domains; "
            f"review data/load/auto_companies.json before deleting anything.")
    return EXIT_FAILURES if loader.failures else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
