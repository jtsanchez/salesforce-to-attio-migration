# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Rules

- This repo migrates a property agency's CRM from Salesforce to Attio.
- The Salesforce org is aliased `source-sf`. It's a Developer Edition org with demo data only.
- Always pass `--target-org source-sf` to every `sf` command. Never touch any other org.
- Use Python 3 for scripts.
- Keep secrets in `.env` and never commit it.

## Repository state

- `sf-source/`: Salesforce DX project holding the tenancy custom fields deployed to `source-sf`.
- `seed/`: Python scripts that generate and load messy demo data into `source-sf`.
- `migrate/`: migration stages. So far only the Salesforce extract (`extract_sf.py`) exists.
- `lib/sfcli.py`: the shared `sf` CLI wrapper used by both `seed/` and `migrate/`.
- `data/` (gitignored): extract output.

There is no transform or Attio-side code yet. Don't assume an architecture beyond what's described here.

## Python

Python 3.12 was installed with winget at `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. Open a new shell before relying on `python` from PATH. Scripts use only the standard library. Run them with `python -I`; each script adds its own folder to `sys.path` so sibling imports still work.

## Source org data model (`source-sf`)

- **Account:** a landlord (an individual, a family trust or a Pty Ltd).
- **Contact:** a landlord or a tenant (`Title` is `Landlord` or `Tenant`).
- **Opportunity:** a tenancy. The property address is only in `Name` and `Description`.
- **OpportunityContactRole** (`Role = Tenant`, `IsPrimary` marks the primary tenant) links tenants to tenancies.
- **Task:** related to a tenancy (`WhatId`) and, usually, to its tenant (`WhoId`).

Custom fields on Opportunity: `Weekly_Rent__c`, `Bond__c`, `Lease_Start_Date__c` (Date), `Lease_Start_Text__c` (legacy dates stored as text in mixed formats) and `Property_State__c` (an unrestricted picklist of state codes that also carries legacy values like `Victoria` and `N.S.W.`).

Field access comes from the `Tenancy_Migration` permission set. For activity custom fields, grant permissions on `Task.X__c` and `Event.X__c`; an `Activity.X__c` entry is silently ignored.

Salesforce behaviours to keep in mind when generating or migrating data:
- The standard Email field is lowercased on save, so emails can't differ by case in the org.
- Unrestricted picklist values are matched case-insensitively to defined values, so `Qld` is stored as `QLD`.
- State and country picklists are on, so address CSVs must use `BillingStateCode`/`BillingCountryCode` (`NSW`, `AU`).
- The standard Contact duplicate rule is active. It rejects a contact that matches an already-saved one with `DUPLICATES_DETECTED`, but not duplicates inside the same batch. Bulk API can't bypass it, so `load.py` upserts Contacts through the REST composite API with `Sforce-Duplicate-Rule-Header: allowSave=true` and leaves the rule switched on.

## Seed data (`seed/`)

Seeded records have `Seed_Key__c` set: `SEED-A###` for Accounts, `SEED-CL###`, `SEED-CT###`, `SEED-CD###` and `SEED-CX###` for landlord, tenant, simple-duplicate and harder-duplicate Contacts, `SEED-O###` for Opportunities and `SEED-T###` for Tasks. The 4 pre-existing Accounts and 4 Contacts have no key and must stay untouched.

```bash
python -I seed/generate.py        # deterministic (seed 42); writes seed/out/*.csv and manifest.json
python -I seed/load.py            # deploys force-app, assigns the permission set, bulk-upserts, then links Tasks/contact roles. Safe to rerun
python -I seed/verify.py          # compares the org with manifest.json; exits 1 on any mismatch
python -I seed/teardown.py --yes  # deletes every record with a Seed_Key__c
```

The `SEED-CX` contacts are generated after everything else so they don't change the random sequence used for the earlier records. Add new seed groups the same way. `seed/metrics.py` holds the duplicate-detection logic used by both `generate.py` (expected counts) and `verify.py` (actual counts).

`lib/sfcli.py` wraps the `sf` CLI and always adds `--target-org source-sf`. It refuses calls that pass their own target org, so route every `sf` call from Python through it. Scripts add `lib/` to `sys.path` themselves. The wrapper provides:
- `run()`, which returns the `--json` result
- `rest()`, raw REST through `sf api request rest`, which raises on Salesforce error lists
- `describe()`, `query()` and `count()`
- `API_VERSION`

## Extract (`migrate/extract_sf.py`)

```bash
python -I migrate/extract_sf.py [--batch-size 200]   # writes data/raw/
python -I migrate/test_extract_sf.py -v              # offline pagination tests (don't use `-m unittest <path>` with -I)
```

- **Objects:** Account, Contact, Opportunity, Task and OpportunityContactRole, with every record in the org (seeded or not).
- **Fields:** every field comes from describe, except compound `address`/`location` fields, whose parts are selected individually.
- **Querying:** through REST `queryAll` with `IsDeleted = false`, so archived Tasks are included. OpportunityContactRole uses `query`. Pages are followed via `nextRecordsUrl`, and the run fails if the collected count doesn't equal `totalSize`.
- **Output:** values are written exactly as Salesforce returns them.
  - `data/raw/<name>.json`: the records
  - `data/raw/<name>.fields.json`: trimmed describe, including picklist values, used for mapping
  - `data/raw/manifest.json`: counts, `extracted_at`, `org_id` and the API version
- **Writes:** each file is written atomically (`.tmp` then rename).

## sf-source/ (Salesforce DX project)

Run all npm and `sf` commands from inside `sf-source/`. `package.json`, `sfdx-project.json` and the tooling config all live there, not at the repo root.

- Package directory: `force-app` (default). Metadata lives under `force-app/main/default/`.
- API version: `sourceApiVersion` 67.0, login URL `https://login.salesforce.com`, no namespace.
- `config/project-scratch-def.json`: Developer-edition scratch org definition.
- `scripts/apex/*.apex` holds anonymous Apex and `scripts/soql/*.soql` holds saved SOQL queries. Both are placeholders for now.
- `.forceignore` excludes `**/__tests__/**`, `jsconfig.json` and `package.xml` from deploy/retrieve.

### Commands

```bash
npm install                          # also installs husky hooks via "prepare"
npm run lint                         # ESLint, Aura/LWC JS only (**/{aura,lwc}/**/*.js)
npm test                             # LWC Jest unit tests (sfdx-lwc-jest)
npm run test:unit -- path/to/foo.test.js   # run a single LWC Jest test file
npm run test:unit:watch | test:unit:coverage | test:unit:debug
npm run prettier                     # format Apex, LWC, Aura, XML, JSON, MD, etc.
npm run prettier:verify              # check formatting only

sf org login web --alias source-sf  # one-time: authorize the source org under the expected alias
sf project retrieve start --target-org source-sf   # pull metadata into force-app
sf project deploy start --target-org source-sf     # deploy force-app
sf apex run test --target-org source-sf --tests <ClassName> --result-format human --wait 10   # Apex tests (run in the org, not locally)
sf apex run --file scripts/apex/hello.apex --target-org source-sf
sf data query --file scripts/soql/account.soql --target-org source-sf
```

Apex tests only run in an org. The local Jest setup (`jest.config.js`) covers LWC only.

### Pre-commit hook

`.husky/pre-commit` runs `npm run precommit`, which calls lint-staged. Staged files get `prettier --write`, Aura/LWC JS gets `eslint`, and any change under `lwc/` runs the related Jest tests with `--bail`. Note that `.husky/` is inside `sf-source/` and the git root is the parent directory, so this hook may not be wired up unless husky is configured for that layout.

### Formatting conventions

Prettier (`.prettierrc`) uses `prettier-plugin-apex` and `@prettier/plugin-xml`, with `trailingComma: "none"`. LWC `.html` files use the `lwc` parser.
