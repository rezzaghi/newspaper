"""The whole run, end to end, through the real CLI: init, fetch from fixture feeds, the batches an agent
works on, render, commit, and the page's votes and feed decisions coming back into the next run."""
import json
import unittest

from helpers import DataTest, FeedServer, atom, free_port, opml, page_data, rfc822, rss

class Pipeline(DataTest):
    @classmethod
    def setUpClass(cls):
        cls.feeds = FeedServer({
            "/feeds/systems.xml": (200, "application/rss+xml", rss("Systems Blog", [
                ("Alpha: how a GC works", "https://example.org/alpha", 1, "https://news.example/1"),
                ("Beta: B-trees", "https://example.org/beta", 2),
                ("Epsilon: rates rise", "https://news.example.com/epsilon", 1),
                ("Older post", "https://example.org/older", 20),  # past the first run's 7 days, within later runs' 30
                ("Ancient post", "https://example.org/ancient", 45),  # never fetched
            ])),
            "/feeds/atom.xml": (200, "application/atom+xml", atom("Atom Mag", [
                ("Gamma", "https://atom.example/gamma", 1),
                ("Beta again, same link", "https://example.org/beta", 2),
                ("Phi: a weird fish", "https://atom.example/phi", 3),
            ])),
            "/feeds/pod.xml": (200, "application/rss+xml", '<?xml version="1.0"?><rss version="2.0"><channel>'
                               f'<title>Pod Show</title><item><title>Episode 1</title><pubDate>{rfc822(1)}</pubDate>'
                               '<description>Show notes.</description><enclosure url="https://pod.example/1.mp3" '
                               'type="audio/mpeg" length="1"/></item></channel></rss>'),
            "/broken.xml": (500, "text/plain", "boom"),
            "/site": (200, "text/html", '<html><head><link rel="alternate" type="application/rss+xml" '
                                        'href="/feeds/systems.xml"></head><body>hi</body></html>'),
            "/notfeed": (200, "text/html", "<html><body>no feed here</body></html>"),
        })

    @classmethod
    def tearDownClass(cls):
        cls.feeds.close()

    def start(self):
        u = self.feeds.url
        src = self.tmp / "import.opml"
        src.write_text(opml([("Systems Blog", f"{u}/feeds/systems.xml", "https://systems.example"),
                             ("Atom Mag", f"{u}/feeds/atom.xml", "https://atom.example"),
                             ("Pod Show", f"{u}/feeds/pod.xml", "https://pod.example"),
                             ("Broken", f"{u}/broken.xml", "https://broken.example")]), encoding="utf-8")
        self.run_cli("init", "--opml", src)
        return self.run_cli("fetch")

    def new_items(self):
        return {i["title"]: i for i in self.state("new_items.json")["items"]}

    def test_init_imports_opml_and_rejects_broken_files(self):
        self.start()
        self.assertTrue((self.data / "profile.md").exists())
        self.assertEqual((self.data / "feeds.opml").read_text(encoding="utf-8").count("xmlUrl"), 4)
        self.assertIn("already set up", self.run_cli("init").stdout)
        bad = self.tmp / "bad.opml"
        bad.write_text("<opml><body>", encoding="utf-8")
        self.env["NEWSPAPER_DATA"] = str(self.tmp / "other")
        self.assertNotEqual(self.run_cli("init", "--opml", bad, ok=False).returncode, 0)

    def test_fetch_dedupes_skips_old_items_and_reports_broken_feeds(self):
        out = self.start().stdout
        items = self.new_items()
        self.assertEqual(sorted(items), ["Alpha: how a GC works", "Beta: B-trees", "Episode 1", "Epsilon: rates rise",
                                         "Gamma", "Phi: a weird fish"])  # older posts skipped on a first run, same link counted once
        self.assertIn("6 new items from 4 feeds", out)
        self.assertIn("FAILED Broken", out)
        alpha = items["Alpha: how a GC works"]
        self.assertEqual(alpha["source"], "Systems Blog")
        self.assertEqual(alpha["comments"], "https://news.example/1")
        self.assertEqual(alpha["summary"], "About Alpha: how a GC works.")
        self.assertEqual(self.state("new_items.json")["errors"].keys(), {"Broken"})
        # Podcast episodes are recognized by their audio attachment, and linked to it when they have no page.
        self.assertEqual((items["Episode 1"]["kind"], items["Episode 1"]["link"]), ("podcast", "https://pod.example/1.mp3"))
        self.assertEqual({items[t]["kind"] for t in items if t != "Episode 1"}, {"text"})

    def test_full_run(self):
        self.start()
        ids = {t: i["id"] for t, i in self.new_items().items()}

        # Triage: the agent keeps, drops and marks surprise candidates.
        triage_in = self.run_cli("batches", "triage").stdout.split()
        self.assertEqual(len(triage_in), 1)
        self.assertEqual(len(json.loads((self.tmp / triage_in[0]).read_text(encoding="utf-8"))), 6)
        self.write_json("state/work/triage_out_1.json", [
            {"id": ids["Alpha: how a GC works"], "keep": True, "section": "article"},
            {"id": ids["Beta: B-trees"], "keep": True, "section": "article"},
            {"id": ids["Epsilon: rates rise"], "keep": True, "section": "news"},
            {"id": ids["Episode 1"], "keep": True, "section": "podcast"},
            {"id": ids["Gamma"], "keep": False, "reason": "promo", "note": "ad"},
            {"id": ids["Phi: a weird fish"], "keep": False, "reason": "off-topic", "surprise": True},
        ])

        # Deep read: kept items and surprise candidates, with full item data.
        read_in = self.run_cli("batches", "read").stdout.split()
        self.assertEqual(len(read_in), 1)
        batch = {i["title"]: i for p in read_in for i in json.loads((self.tmp / p).read_text(encoding="utf-8"))}
        self.assertEqual(sorted(batch), ["Alpha: how a GC works", "Beta: B-trees", "Episode 1", "Epsilon: rates rise",
                                         "Phi: a weird fish"])
        self.assertTrue(batch["Phi: a weird fish"]["surprise"])
        self.assertNotIn("surprise", batch["Alpha: how a GC works"])
        self.assertEqual(batch["Alpha: how a GC works"]["link"], "https://example.org/alpha")

        section = {"Alpha: how a GC works": "article", "Beta: B-trees": "article", "Epsilon: rates rise": "news",
                   "Episode 1": "podcast", "Phi: a weird fish": "surprise"}
        reads = [{"id": i["id"], "keep": t != "Beta: B-trees", "reason": "weak-argument" if t == "Beta: B-trees" else "",
                  "note": "", "section": section[t], "title": t, "summary": "S.", "why_you": "W.", "quality": "solid",
                  "credibility": "n/a", "impact": "high" if section[t] == "news" else None, "read_minutes": 3}
                 for t, i in batch.items()]
        self.write_json("state/work/read_out_1.json", reads)

        # Edit: the agent writes the digest and render builds the page from it.
        kept = [r for r in reads if r["keep"] and r["title"] != "Episode 1"]  # the editor cuts the episode
        self.write_json("state/digest.json", {"intro": "Intro.", "items": kept})
        self.run_cli("render")
        page = page_data((self.data / "digest.html").read_text(encoding="utf-8"))
        self.assertEqual(sorted((i["title"], i["section"]) for i in page["items"]),
                         [("Alpha: how a GC works", "article"), ("Epsilon: rates rise", "news"),
                          ("Phi: a weird fish", "surprise")])
        alpha = next(i for i in page["items"] if i["title"].startswith("Alpha"))
        self.assertEqual((alpha["source"], alpha["link"], alpha["comments"]),
                         ("Systems Blog", "https://example.org/alpha", "https://news.example/1"))
        dropped = {d["title"]: d["reason"] for d in page["dropped"]}
        self.assertEqual(dropped, {"Beta: B-trees": "weak-argument", "Gamma": "promo", "Episode 1": "cut by editor"})
        self.assertEqual((page["scanned"], page["feeds"], page["intro"]), (6, 4, "Intro."))

        # Commit: everything this run is seen, kept items are logged, work files are gone.
        self.run_cli("commit")
        self.assertEqual(set(self.state("seen.json")), set(ids.values()))
        log = [json.loads(l) for l in (self.data / "state" / "kept.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(k["title"] for k in log), ["Alpha: how a GC works", "Epsilon: rates rise", "Phi: a weird fish"])
        self.assertFalse((self.data / "state" / "work").exists())
        self.assertFalse((self.data / "state" / "new_items.json").exists())
        self.assertIn("1 new items", self.run_cli("fetch").stdout)  # seen items never come back
        self.assertEqual([i["title"] for i in self.state("new_items.json")["items"]], ["Older post"])

        # Signals: sites the feeds keep linking out to become feed candidates.
        self.assertIn("example.org", self.run_cli("signals").stdout)

    def test_votes_from_the_page_are_learned_once(self):
        self.start()
        self.write_json("state/votes.json", {"a1": {"vote": "up", "title": "T1"}, "b2": {"vote": "down", "title": "T2"}})
        self.assertEqual(set(json.loads(self.run_cli("votes").stdout)), {"a1", "b2"})
        self.assertIn("2 votes marked", self.run_cli("votes", "--mark").stdout)
        self.assertEqual(json.loads(self.run_cli("votes").stdout), {})
        self.write_json("state/votes.json", {"a1": {"vote": "up"}, "b2": {"vote": "down"}, "c3": {"vote": "up"}})
        self.assertEqual(set(json.loads(self.run_cli("votes").stdout)), {"c3"})

    def test_feed_suggestions_and_decisions(self):
        self.start()
        u = self.feeds.url
        picks = self.write_json("picks.json", [
            {"name": "Systems Blog again", "site": "x", "feed_url": f"{u}/feeds/systems.xml"},  # already followed
            {"name": "New Mag", "site": "https://new.example", "feed_url": "https://new.example/feed", "why": "Good."},
            {"name": "Other Mag", "site": "https://other.example", "feed_url": "https://other.example/feed"},
        ])
        self.assertIn("2 new suggestions", self.run_cli("suggest", picks).stdout)
        self.assertIn("0 new suggestions", self.run_cli("suggest", picks).stdout)
        self.assertIn("no (already ran", self.run_cli("discover-due").stdout)
        sugg = {s["name"]: s for s in self.state("feed_suggestions.json")}
        self.write_json("state/feed_decisions.json", {sugg["New Mag"]["id"]: {"decision": "add"},
                                                      sugg["Other Mag"]["id"]: {"decision": "skip"}})
        out = self.run_cli("apply-decisions").stdout
        self.assertIn("New Mag: added", out)
        self.assertIn("Other Mag: skipped", out)
        self.assertEqual(self.run_cli("apply-decisions").stdout.strip(), "")  # applying twice changes nothing
        feeds = (self.data / "feeds.opml").read_text(encoding="utf-8")
        self.assertEqual(feeds.count("https://new.example/feed"), 1)
        self.assertNotIn("other.example", feeds)
        self.assertTrue(feeds.rstrip().endswith("</opml>"))

    def test_first_run_scout_picks_are_followed_directly(self):
        self.run_cli("init")
        picks = self.write_json("picks.json", [{"name": "Systems Blog", "site": "s", "feed_url": f"{self.feeds.url}/feeds/systems.xml"}])
        self.assertIn("1 feeds added; following 1", self.run_cli("suggest", picks, "--add").stdout)
        self.assertIn("3 new items from 1 feeds", self.run_cli("fetch").stdout)

    def test_probe_finds_feeds_behind_html_pages(self):
        u = self.feeds.url
        self.run_cli("init")
        found, missing = json.loads(self.run_cli("probe", f"{u}/site", f"{u}/notfeed").stdout)
        self.assertEqual(found["feed_url"], f"{u}/feeds/systems.xml")
        self.assertEqual(found["title"], "Systems Blog")
        self.assertEqual(len(found["samples"]), 5)
        self.assertIsNotNone(found["per_week"])
        self.assertIn("no feed found", missing["error"])

    def test_no_network_is_reported_as_such(self):
        dead = free_port()
        src = self.tmp / "dead.opml"
        src.write_text(opml([("A", f"http://127.0.0.1:{dead}/a", ""), ("B", f"http://127.0.0.1:{dead}/b", "")]),
                       encoding="utf-8")
        self.run_cli("init", "--opml", src)
        r = self.run_cli("fetch", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("NO NETWORK", r.stderr)
        self.assertIsNone(self.state("new_items.json"))


if __name__ == "__main__":
    unittest.main()
