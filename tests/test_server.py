"""The local page server over real HTTP: storing the page's clicks, and `open` replacing servers that can't do the job."""
import json
import subprocess
import sys
import time
import unittest

from helpers import ROOT, DataTest, FeedServer, free_port, http, kill


class Server(DataTest):
    def setUp(self):
        super().setUp()
        self.run_cli("init")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.procs, self.pids = [], []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
            p.stderr.close()
        for pid in self.pids:
            kill(pid)
        super().tearDown()

    def serve(self, patch="pass"):
        """Start a page server, optionally with module patches applied first (to fake an old or sandboxed server)."""
        # If the server is still not answering after 10s, faulthandler prints where it is stuck.
        code = (f"import faulthandler, sys; faulthandler.dump_traceback_later(10); sys.path.insert(0, {str(ROOT)!r}); "
                f"import newspaper as n; {patch}; print('listening', flush=True, file=sys.stderr); "
                f"n.PageServer(('127.0.0.1', {self.port}), n.PageHandler).serve_forever()")
        proc = subprocess.Popen([sys.executable, "-c", code], env=self.env, cwd=self.tmp,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        deadline, error = time.monotonic() + 15, None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(f"server exited:\n{proc.stderr.read()}")
            try:
                return json.loads(http("GET", f"{self.base}/api/ping")[1])
            except Exception as e:
                error = e
                time.sleep(0.1)
        proc.kill()
        self.fail(f"server did not answer within 15s; last error: {error!r}\nserver stderr:\n{proc.communicate()[1]}")

    def ping(self):
        return json.loads(http("GET", f"{self.base}/api/ping")[1])

    def test_page_and_redirect(self):
        self.serve()
        status, body = http("GET", f"{self.base}/feed")
        self.assertEqual(status, 200)
        self.assertIn("No paper yet", body)
        self.write("digest.html", "<p>my paper</p>")
        self.assertEqual(http("GET", f"{self.base}/feed")[1], "<p>my paper</p>")
        self.assertEqual(http("GET", f"{self.base}/")[1], "<p>my paper</p>")  # / redirects to /feed
        self.assertEqual(http("GET", f"{self.base}/nothing")[0], 404)

    def test_clicks_are_stored_in_data_state(self):
        self.serve()
        for col in ("votes", "saved", "feed_decisions"):
            self.assertEqual(http("GET", f"{self.base}/api/{col}"), (200, "{}"))
        self.assertEqual(http("PUT", f"{self.base}/api/votes/abc", {"vote": "up", "title": "Ação"})[0], 200)
        self.assertEqual(http("PUT", f"{self.base}/api/votes/abc", {"vote": "down", "title": "Ação"})[0], 200)
        self.assertEqual(http("PUT", f"{self.base}/api/saved/id%20with%20space", {"title": "S"})[0], 200)
        self.assertEqual(self.state("votes.json"), {"abc": {"vote": "down", "title": "Ação"}})
        self.assertEqual(json.loads(http("GET", f"{self.base}/api/saved")[1]), {"id with space": {"title": "S"}})
        self.assertEqual(http("DELETE", f"{self.base}/api/saved/id%20with%20space")[0], 200)
        self.assertEqual(self.state("saved.json"), {})
        self.assertEqual(http("DELETE", f"{self.base}/api/saved/never-existed")[0], 200)

    def test_rejects_bad_requests(self):
        self.serve()
        self.assertEqual(http("PUT", f"{self.base}/api/secrets/x", {"a": 1})[0], 404)  # unknown collection
        self.assertEqual(http("PUT", f"{self.base}/api/../../profile/x", {"a": 1})[0], 404)
        self.assertEqual(http("PUT", f"{self.base}/api/votes", {"a": 1})[0], 404)  # no doc id
        self.assertEqual(http("PUT", f"{self.base}/api/votes/x", b"not json")[0], 400)
        self.assertEqual(http("GET", f"{self.base}/api/votes/x")[0], 404)
        self.assertFalse((self.data / "state" / "secrets.json").exists())

    def test_ping_identifies_the_server(self):
        self.serve()
        info = self.ping()
        self.assertEqual(info["app"], "newspaper")
        self.assertTrue(info["writable"])
        self.assertEqual(info["pid"], self.procs[0].pid)

    def open(self, ok=True):
        r = self.run_cli("open", "--no-browser", "--port", self.port, ok=ok)
        if r.returncode == 0:
            self.pids.append(self.ping()["pid"])
        return r

    def test_open_starts_a_server_and_reuses_it(self):
        out = self.open().stdout.strip()
        self.assertEqual(out, f"http://newspaper.localhost:{self.port}/feed")
        first = self.ping()["pid"]
        self.open()
        self.assertEqual(self.ping()["pid"], first)

    def test_open_replaces_a_server_running_old_code(self):
        self.serve("n.VERSION = 'old'")
        old = self.ping()
        self.open()
        new = self.ping()
        self.assertNotEqual(new["pid"], old["pid"])
        self.assertNotEqual(new["version"], "old")

    def test_open_replaces_a_server_that_cannot_save(self):
        self.serve("n.can_write = lambda: False")  # what a server started inside an agent's sandbox looks like
        self.assertFalse(self.ping()["writable"])
        self.open()
        self.assertTrue(self.ping()["writable"])

    def test_open_explains_servers_it_cannot_replace(self):
        legacy = FeedServer({"/api/ping": (200, "application/json", '{"app": "newspaper"}')})
        self.port = int(legacy.url.rsplit(":", 1)[1])
        try:
            r = self.open(ok=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("old newspaper server", r.stderr)
        finally:
            legacy.close()
        other = FeedServer({})
        self.port = int(other.url.rsplit(":", 1)[1])
        try:
            r = self.open(ok=False)
            self.assertIn("used by another program", r.stderr)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
