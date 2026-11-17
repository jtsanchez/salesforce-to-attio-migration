"""Thin wrapper around the Salesforce CLI. Every call is pinned to the source-sf org.

Shared by seed/ and migrate/. Import it after adding this folder to sys.path
(scripts run under `python -I`, which leaves the script's own folder off the path).
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

TARGET_ORG = "source-sf"
API_VERSION = "v68.0"
ROOT = Path(__file__).resolve().parent.parent
SF_PROJECT = ROOT / "sf-source"


def _sf_exe():
    exe = shutil.which("sf")
    if not exe:
        sys.exit("Salesforce CLI 'sf' not found on PATH")
    return exe


def run(args, cwd=SF_PROJECT):
    """Run `sf <args> --target-org source-sf --json` and return the parsed JSON."""
    if "--target-org" in args:
        raise ValueError("target org is fixed to source-sf; do not pass --target-org")
    cmd = [_sf_exe(), *args, "--target-org", TARGET_ORG, "--json"]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"sf {' '.join(args)} returned non-JSON output:\n{proc.stdout}\n{proc.stderr}")
    if data.get("status", 1) != 0:
        raise RuntimeError(f"sf {' '.join(args)} failed: {data.get('message')}\n{json.dumps(data, indent=2)[:4000]}")
    return data.get("result", {})


def rest(method, path, body_file=None, headers=()):
    """Raw REST call via `sf api request rest`; returns the parsed JSON response body.

    Raises RuntimeError when Salesforce returns an error list ([{"errorCode": ..., "message": ...}]).
    Per-record results (e.g. composite upserts, which carry "success") are returned as-is.
    """
    args = [_sf_exe(), "api", "request", "rest", path, "--method", method, "--target-org", TARGET_ORG]
    for h in headers:
        args += ["--header", h]
    if body_file:
        args += ["--body", f"@{body_file}"]
    proc = subprocess.run(args, cwd=SF_PROJECT, capture_output=True, text=True, encoding="utf-8")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"sf api request rest {method} {path} failed:\n{proc.stdout}\n{proc.stderr}")
    if (isinstance(data, list) and data and isinstance(data[0], dict)
            and "errorCode" in data[0] and "success" not in data[0]):
        raise RuntimeError(f"REST {method} {path[:200]} failed: "
                           + "; ".join(f"{e.get('errorCode')}: {e.get('message')}" for e in data))
    return data


def describe(sobject):
    """Full describe result for an sObject."""
    return run(["sobject", "describe", "--sobject", sobject])


def query(soql):
    """Return all records for a SOQL query (attributes stripped)."""
    result = run(["data", "query", "--query", soql])
    records = result.get("records", [])
    for r in records:
        r.pop("attributes", None)
    return records


def count(soql_where_object):
    """`count("Contact WHERE ...")` -> int, via SELECT COUNT()."""
    result = run(["data", "query", "--query", f"SELECT COUNT() FROM {soql_where_object}"])
    return result["totalSize"]
