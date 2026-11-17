"""Offline tests for extract_sf pagination (the real org is too small to page).

Run: python -I migrate/test_extract_sf.py -v
(`python -I -m unittest <path>` can't import it: -I drops the cwd from sys.path.)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_sf  # noqa: E402


def fake_rest(pages):
    """Return a rest() stand-in serving `pages` in order and recording requested paths."""
    calls = []

    def rest(method, path, body_file=None, headers=()):
        calls.append((method, path, list(headers)))
        return pages[len(calls) - 1]

    return rest, calls


def page(ids, total, next_url=None):
    return {"totalSize": total, "done": next_url is None, "nextRecordsUrl": next_url,
            "records": [{"attributes": {"type": "Contact"}, "Id": i} for i in ids]}


class FetchAllTests(unittest.TestCase):
    def test_follows_next_records_url_across_pages(self):
        rest, calls = fake_rest([
            page(["a", "b"], 5, "/services/data/v68.0/query/01g-2"),
            page(["c", "d"], 5, "/services/data/v68.0/query/01g-4"),
            page(["e"], 5),
        ])
        records, pages = extract_sf.fetch_all("SELECT Id FROM Contact", batch_size=200, rest=rest)
        self.assertEqual([r["Id"] for r in records], ["a", "b", "c", "d", "e"])
        self.assertEqual(pages, 3)
        self.assertTrue(all("attributes" not in r for r in records))
        self.assertIn("/queryAll?q=SELECT%20Id%20FROM%20Contact", calls[0][1])
        self.assertEqual(calls[1][1], "/services/data/v68.0/query/01g-2")
        self.assertEqual(calls[0][2], ["Sforce-Query-Options: batchSize=200"])

    def test_total_size_mismatch_fails(self):
        rest, _ = fake_rest([page(["a"], 2)])
        with self.assertRaises(extract_sf.ExtractError):
            extract_sf.fetch_all("SELECT Id FROM Contact", rest=rest)

    def test_not_done_without_next_url_fails(self):
        bad = page(["a"], 2)
        bad["done"] = False
        rest, _ = fake_rest([bad])
        with self.assertRaises(extract_sf.ExtractError):
            extract_sf.fetch_all("SELECT Id FROM Contact", rest=rest)

    def test_query_endpoint_when_query_all_disabled(self):
        rest, calls = fake_rest([page(["a"], 1)])
        extract_sf.fetch_all("SELECT Id FROM OpportunityContactRole", query_all=False, rest=rest)
        self.assertIn("/query?q=", calls[0][1])


if __name__ == "__main__":
    unittest.main()
