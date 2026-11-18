# sf-to-attio

Tooling to migrate a property agency's CRM from Salesforce to Attio. The source is a Salesforce Developer Edition org (alias `source-sf`) seeded with deliberately messy demo data. Landlords, tenants, tenancies and tasks are mapped to Attio companies, people, deals and tasks. The seed and extract stages have been run against `source-sf`, and the transform and the loader's dry run have been run offline on that extract. **The Attio load has not yet been run against any live Attio workspace** (see [Attio loader](#attio-loader)).

## Pipeline

| Stage | Command | Output | Talks to | Status |
|---|---|---|---|---|
| Seed demo data | `python -I seed/load.py` | records in `source-sf` | Salesforce (`source-sf` only) | run against `source-sf` |
| Extract | `python -I migrate/extract_sf.py` | `data/raw/` | Salesforce (read only) | run against `source-sf` |
| Transform | `.venv/Scripts/python -I migrate/transform.py` | `data/transformed/` (+ `report.md`, `review.csv`) | nothing (offline) | run on the real extract |
| Load (dry run) | `.venv/Scripts/python -I migrate/load_attio.py` | `data/load/dry_run_plan.json` | nothing (offline) | run on the real transform output |
| Load (apply) | `.venv/Scripts/python -I migrate/load_attio.py --apply` | would write records to Attio, plus `data/load/` | Attio REST API | **tested against mocks only; never run live** |

`migrate/mapping.yaml` is the spec for the transform and load: field mapping, cleaning rules, duplicate rules, Attio setup and expected counts. `data/` is gitignored.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # PyYAML + pytest, pinned
.venv/Scripts/python -I -m pytest                             # all tests, no network
cp .env.example .env                                           # then fill in; .env is gitignored
```

## Attio loader

> **⚠ Tested against mocked HTTP only.** `migrate/load_attio.py` has **not** been run against a live Attio workspace. Its tests (`migrate/tests/test_load_attio.py`) run against an in-memory fake that follows the request and response shapes in the [Attio REST API docs](https://docs.attio.com/rest-api/overview), with sockets blocked. Passing tests show only that the loader's own logic behaves as intended against that fake: ordering, reference resolution, batching, retries, the guard and the audit. They do **not** show that Attio accepts these requests or responds the way the fake does. Everything below describes how the code is *designed* to behave. Run it against a **sandbox workspace** before any real one.

**What it does.**
- Dry run is the default. It makes no HTTP requests, needs no API key, and writes the planned requests to `data/load/dry_run_plan.json`.
- `--apply` runs in this order:
  1. Pre-flight checks, then creating any missing custom attributes and select options.
  2. Upserting companies, people and deals on `salesforce_id`.
  3. Creating tasks, or updating tasks from an earlier run.
  4. The auto-company audit.

**Batching and retries.**
- Attio has no bulk record endpoint, so each record is one request. Requests are grouped into batches of `--batch-size` (default 50).
- `data/load/state.json`, which maps Salesforce IDs to Attio IDs, is saved after every batch, so a rerun resumes and updates instead of duplicating.
- Writes are paced at 20 per second, under Attio's documented limit of 25.
- 429 responses wait until the `Retry-After` date.
- 5xx responses, 408 responses and dropped connections back off exponentially with jitter, up to 5 retries.
- Other 4xx errors aren't retried. They're recorded per record in `data/load/last_run.json`, and the run carries on.

**Auto-company guard.** Attio documents that creating a person "generates or matches" a company from the person's email domain. Every email in the demo data is `example.com`, `.net` or `.org`, so a load could create junk companies.
- **Before loading:** `--apply` is blocked when people use reserved test domains (`example.*`, `.test`, `.invalid`, `.localhost`), or a non-company domain shared by 3 or more people (for example, a free-mail provider). Override with `--accept-auto-company-risk`, and only in a sandbox.
- **After loading:** companies with no `salesforce_id` whose domain matches one of those email domains are written to `data/load/auto_companies.json`. Nothing is deleted automatically.

**Other safety checks.**
- The state file records the workspace ID, and a run against a different workspace with the same state file is refused.
- The API key is sent only in the `Authorization` header and is never logged.
- The transform's `_migration` metadata is never sent.

**Assumptions not yet verified against a live workspace:**
- The `GET /v2/self` response carries `workspace_id`.
- The `config` shapes used when creating currency and record-reference attributes (`{"currency": {...}}`, `{"record_reference": {...}}`).
- The statuses endpoint (`/v2/objects/deals/attributes/stage/statuses`) and the options endpoint.
- Upserting companies on `salesforce_id`, which can't be unique on Companies (Attio rejects a value matching more than one record).
- Whether auto-company creation applies to API-created people, and whether setting `company` explicitly prevents it.
- Which task fields `PATCH /v2/tasks/{id}` accepts. The loader sends only `deadline_at`, `is_completed`, `linked_records` and `assignees`.
- The deal `value` currency: the loader can't set it, so set it to AUD by hand.

**Checklist for the first live run (not yet done; use a sandbox workspace):**
1. Enable the **Deals** object (Settings → Objects) and set the deal value currency to **AUD**.
2. Put `ATTIO_API_KEY` and `ATTIO_DEFAULT_OWNER_EMAIL` in `.env`.
3. Run `.venv/Scripts/python -I migrate/load_attio.py` and read `data/load/dry_run_plan.json`.
4. Run `.venv/Scripts/python -I migrate/load_attio.py --apply --accept-auto-company-risk --batch-size 10`.
5. Check `data/load/last_run.json` (failures) and `data/load/auto_companies.json`. Then check the counts in Attio against `data/transformed/report.md`: 44 companies, 84 people, 65 deals, 50 tasks.
6. Fix any of the assumptions above that turned out wrong, then update this section.
