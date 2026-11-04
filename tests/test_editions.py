"""Editions: every run's page is kept (the newest 10) and served at its own link, with a switcher between them."""
import json
import re
import unittest
from datetime import datetime, timedelta, timezone

from helpers import DataTest, free_port, http, kill, page_data
import test_page
from test_page import CHROME


class EditionData(DataTest):
    """Renders runs the way an agent would, without fetching."""

    def setUp(self):
        super().setUp()
        self.run_cli("init")

    def render_run(self, hours_ago, n_items=2, intro="Intro."):
        fetched = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(microsecond=0)
        items = [{"id": f"{hours_ago}-{k}", "title": f"Story {k} from {hours_ago}h ago", "source": "Src",
                  "link": f"https://example.org/{hours_ago}/{k}", "comments": "", "published": None, "kind": "text",
                  "summary": ""} for k in range(n_items)]
        self.write_json("state/new_items.json", {"fetched_at": fetched.isoformat(), "feeds": 3, "items": items})
        self.write_json("state/digest.json", {"intro": intro, "items": [
            {"id": i["id"], "section": "article", "title": i["title"], "summary": "S."} for i in items]})
        self.run_cli("render")
        return fetched.astimezone().strftime("%Y-%m-%d-%H%M%S")

    def kept_editions(self):
        return sorted(p.stem for p in (self.data / "editions").glob("*.json"))


class Editions(EditionData):
    def test_each_run_is_an_edition_and_rerendering_replaces_it(self):
        first = self.render_run(5)
        self.write_json("state/digest.json", {"intro": "Edited again.", "items": []})
        self.run_cli("render")  # same run, rendered twice
        self.assertEqual(self.kept_editions(), [first])
        self.assertEqual(self.state(f"../editions/{first}.json")["intro"], "Edited again.")
        second = self.render_run(1)
        self.assertEqual(self.kept_editions(), [first, second])

    def test_only_the_newest_ten_are_kept(self):
        made = [self.render_run(h) for h in range(30, 18, -1)]  # 12 runs, oldest first
        self.assertEqual(self.kept_editions(), made[-10:])

    def test_page_lists_editions_newest_first(self):
        old, new = self.render_run(26, n_items=3), self.render_run(2, n_items=1)
        page = page_data((self.data / "digest.html").read_text(encoding="utf-8"))
        self.assertEqual(page["edition"], new)
        self.assertEqual([(e["id"], e["kept"], e["scanned"]) for e in page["editions"]], [(new, 1, 1), (old, 3, 3)])
        self.assertTrue(page["editions"][1]["fetched_at"] < page["editions"][0]["fetched_at"])


class EditionServer(EditionData):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        try:
            kill(json.loads(http("GET", f"{self.base}/api/ping")[1])["pid"])
        except Exception:
            pass
        super().tearDown()

    def test_each_edition_has_its_own_link(self):
        old, new = self.render_run(26, intro="Old intro."), self.render_run(2, intro="New intro.")
        self.run_cli("open", "--no-browser", "--port", self.port)
        status, html = http("GET", f"{self.base}/feed")
        self.assertEqual((status, page_data(html)["intro"]), (200, "New intro."))
        status, html = http("GET", f"{self.base}/feed/{old}")
        self.assertEqual((status, page_data(html)["intro"]), (200, "Old intro."))
        self.assertEqual(page_data(html)["editions"][0]["id"], new)  # an old edition still knows about newer ones
        self.assertEqual(http("GET", f"{self.base}/feed/2020-01-01-000000")[0], 404)
        self.assertEqual(http("GET", f"{self.base}/feed/..%2f..%2fprofile")[0], 404)

    def test_old_editions_show_current_feed_suggestions(self):
        old = self.render_run(26)
        self.write_json("state/feed_suggestions.json", [
            {"id": "s1", "status": "pending", "name": "New Mag", "feed_url": "https://new.example/feed"},
            {"id": "s2", "status": "added", "name": "Old Mag", "feed_url": "https://old.example/feed"}])
        self.run_cli("open", "--no-browser", "--port", self.port)
        page = page_data(http("GET", f"{self.base}/feed/{old}")[1])
        self.assertEqual([s["name"] for s in page["suggestions"]], ["New Mag"])


@unittest.skipUnless(CHROME, "Chrome not installed")
class EditionSwitcher(EditionData):
    dom = test_page.Page.dom
    text = staticmethod(test_page.Page.text)

    def setUp(self):
        super().setUp()
        self.ids = [self.render_run(h) for h in (50, 26, 2)]  # oldest first
        self.port = free_port()
        self.run_cli("open", "--no-browser", "--port", self.port)

    def tearDown(self):
        kill(json.loads(http("GET", f"http://127.0.0.1:{self.port}/api/ping")[1])["pid"])
        super().tearDown()

    def nav(self, html):
        older = re.search(r'<a id="older"([^>]*)>', html).group(1)
        newer = re.search(r'<a id="newer"([^>]*)>', html).group(1)
        return older, newer

    def test_latest_edition(self):
        self.url = f"http://127.0.0.1:{self.port}/feed"
        html = self.dom()
        self.assertNotRegex(html, r'<nav class="editions" id="editions"[^>]*hidden')
        older, newer = self.nav(html)
        self.assertIn(f'href="/feed/{self.ids[1]}"', older)
        self.assertIn("hidden", newer)
        self.assertRegex(html, r'<p class="old-edition" id="old-edition" hidden')
        self.assertEqual(len(re.findall(r'<li><a href="/feed', html)), 3)
        self.assertRegex(html, r'<a href="/feed" aria-current="page">')

    def test_older_edition(self):
        self.url = f"http://127.0.0.1:{self.port}/feed/{self.ids[1]}"
        html = self.dom()
        older, newer = self.nav(html)
        self.assertIn(f'href="/feed/{self.ids[0]}"', older)
        self.assertIn('href="/feed"', newer)
        self.assertNotIn("hidden", newer)
        text = self.text(html)
        self.assertRegex(text, r"An earlier edition, from (yesterday|today|\w{3}, \w+ \d+|\w{3}, \d+ \w+),")
        self.assertIn("Latest edition →", text)
        self.assertRegex(html, rf'<a href="/feed/{self.ids[1]}" aria-current="page">')


if __name__ == "__main__":
    unittest.main()
