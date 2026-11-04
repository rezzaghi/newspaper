"""Against the real internet: real feeds still parse and probe still finds them. Feeds change and go down, so this
only runs when NEWSPAPER_LIVE=1 (CI runs it daily and on every push, without blocking)."""
import json
import os
import unittest

from helpers import DataTest

LIVE = os.environ.get("NEWSPAPER_LIVE") == "1"
FEEDS = [("Lobsters", "https://lobste.rs/rss", "https://lobste.rs"),
         ("Quanta Magazine", "https://www.quantamagazine.org/feed/", "https://www.quantamagazine.org")]


@unittest.skipUnless(LIVE, "set NEWSPAPER_LIVE=1 to test against real feeds")
class Live(DataTest):
    def test_real_feeds_fetch(self):
        self.run_cli("init")
        picks = self.write_json("picks.json", [{"name": n, "feed_url": f, "site": s} for n, f, s in FEEDS])
        self.run_cli("suggest", picks, "--add")
        out = self.run_cli("fetch").stdout
        items = self.state("new_items.json")["items"]
        self.assertGreater(len(items), 0, out)
        self.assertTrue(all(i["title"] and i["link"].startswith("http") for i in items))
        self.assertNotIn("FAILED", out)

    def test_probe_finds_feeds_from_homepages(self):
        self.run_cli("init")
        results = json.loads(self.run_cli("probe", "https://lobste.rs", "https://www.quantamagazine.org").stdout)
        for r in results:
            self.assertNotIn("error", r, r)
            self.assertTrue(r["samples"])


if __name__ == "__main__":
    unittest.main()
