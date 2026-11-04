"""The rendered page in a real headless Chrome: its JavaScript runs, builds the sections and loads the reader's
saved items and votes from the local server. Skipped when Chrome isn't installed, unless NEWSPAPER_REQUIRE_CHROME=1."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import DataTest, free_port, http, kill


def find_chrome():
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if shutil.which(name):
            return shutil.which(name)
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if Path(path).exists():
            return path
    return None


CHROME = find_chrome()
ITEMS = [
    {"id": "n1", "section": "news", "impact": "high", "title": "Rates rise sharply", "summary": "S.", "why_you": "Money.",
     "changes": "Loans cost more.", "quality": "solid", "credibility": "confirmed", "read_minutes": 4},
    {"id": "n2", "section": "news", "impact": "medium", "title": "Minor policy tweak", "summary": "S.", "why_you": "W.",
     "credibility": "disputed", "credibility_note": "A correction says otherwise.", "read_minutes": 2},
    {"id": "a1", "section": "article", "title": "How garbage collectors work", "summary": "S.", "why_you": "Systems.",
     "arguments": ["Tracing finds live objects."], "conclusion": "Pick by workload.", "quality": "strong", "read_minutes": 9},
    {"id": "p1", "section": "podcast", "title": "An episode on compilers", "summary": "S.", "why_you": "W.", "read_minutes": 50},
    {"id": "s1", "section": "surprise", "format": "video", "title": "A master woodworker", "summary": "S.",
     "why_you": "Craft at its best.", "read_minutes": 12},
]


@unittest.skipUnless(CHROME or os.environ.get("NEWSPAPER_REQUIRE_CHROME"), "Chrome not installed")
class Page(DataTest):
    def setUp(self):
        super().setUp()
        if not CHROME:
            self.fail("NEWSPAPER_REQUIRE_CHROME is set but Chrome was not found (set CHROME to its path)")
        self.run_cli("init")
        self.write_json("state/new_items.json", {"feeds": 7, "items": [
            {"id": i["id"], "title": i["title"], "source": "Src", "link": f"https://example.org/{i['id']}", "comments": "",
             "published": None, "kind": "text", "summary": ""} for i in ITEMS] + [
            {"id": "d1", "title": "A launch post", "source": "Src", "link": "https://example.org/d1", "comments": "",
             "published": None, "kind": "text", "summary": ""}]})
        self.write_json("state/work/triage_out_1.json", [{"id": "d1", "keep": False, "reason": "promo", "note": "launch"}])
        self.write_json("state/digest.json", {"intro": "Read the GC piece first.", "items": ITEMS})
        self.run_cli("render")
        self.port = free_port()
        self.run_cli("open", "--no-browser", "--port", self.port)
        self.url = f"http://127.0.0.1:{self.port}/feed"

    def tearDown(self):
        import json
        kill(json.loads(http("GET", f"http://127.0.0.1:{self.port}/api/ping")[1])["pid"])
        super().tearDown()

    def dom(self):
        profile = tempfile.mkdtemp(prefix="newspaper-chrome-")
        try:
            r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                                f"--user-data-dir={profile}", "--virtual-time-budget=5000", "--dump-dom", self.url],
                               capture_output=True, text=True, encoding="utf-8", timeout=90)
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        self.assertIn("</html>", r.stdout, r.stderr[-2000:])
        return r.stdout

    @staticmethod
    def text(html):
        """Visible text: no scripts, styles or hidden elements' markup."""
        html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    def test_sections_and_cards_are_built(self):
        html = self.dom()
        text = self.text(html)
        tabs = re.findall(r'<button class="stab"[^>]*>(.*?)</button>', html)
        self.assertEqual([re.sub(r"<[^>]+>", " ", t).split()[0] for t in tabs], ["News", "Articles", "Podcasts", "Might"])
        self.assertIn("5 worth your time", text)
        self.assertIn("Read the GC piece first.", text)
        for item in ITEMS:
            self.assertIn(item["title"], text)
        self.assertIn("Could change your life", text)  # news grouped by impact
        self.assertIn("Worth knowing", text)
        self.assertIn("A correction says otherwise.", text)  # disputed claims carry their source
        self.assertIn("Tracing finds live objects.", text)
        self.assertIn("1 dropped, and why", text)
        self.assertIn("promo", text)

    def test_saved_items_and_votes_load_from_the_server(self):
        base = f"http://127.0.0.1:{self.port}"
        http("PUT", f"{base}/api/saved/old1", {"id": "old1", "title": "Kept from last week", "section": "essay",
                                              "saved_at": "2026-01-01T00:00:00Z"})
        http("PUT", f"{base}/api/saved/a1", {**ITEMS[2], "saved_at": "2026-01-02T00:00:00Z"})
        http("PUT", f"{base}/api/votes/n1", {"vote": "up", "title": ITEMS[0]["title"]})
        html = self.dom()
        self.assertIn("Kept from last week", self.text(html))
        self.assertRegex(html, r'id="saved-count"[^>]*>2<')
        self.assertIn("4 worth your time", self.text(html))  # a saved item leaves the digest
        self.assertRegex(html, r'class="vote up"[^>]*aria-pressed="true"')
        self.assertRegex(html, r'<span id="unsynced" hidden')  # nothing waiting to be sent


if __name__ == "__main__":
    unittest.main()
