"""Shared test helpers: run newspaper.py as a real CLI against a throwaway data folder, and serve fixture feeds locally."""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "newspaper.py"
sys.path.insert(0, str(ROOT))

import newspaper  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method, url, body=None):
    """(status, text) of a request to a local server; never raises on HTTP errors."""
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except Exception as e:
        if hasattr(e, "code"):
            return e.code, e.read().decode("utf-8")
        raise


def page_data(html):
    """The JSON the renderer embedded in a page."""
    blob = re.search(r'<script type="application/json" id="digest">(.*?)</script>', html, re.S).group(1)
    return json.loads(blob.replace("<\\/", "</"))


def kill(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


class DataTest(unittest.TestCase):
    """Each test gets an empty data folder; `run` calls the CLI the way an agent does."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="newspaper-test-"))
        self.data = self.tmp / "data"
        self.env = {**os.environ, "NEWSPAPER_DATA": str(self.data), "PYTHONUTF8": "1",
                    "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, ok=True, timeout=120):
        r = subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=self.tmp, env=self.env,
                           capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        if ok and r.returncode != 0:
            self.fail(f"newspaper.py {' '.join(map(str, args))} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r

    def state(self, name, default=None):
        p = self.data / "state" / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    def write(self, rel, text):
        p = self.data / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def write_json(self, rel, data):
        return self.write(rel, json.dumps(data, ensure_ascii=False))


def rfc822(days_ago):
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def rss(title, items):
    """items: (title, link, days_ago[, comments])"""
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><pubDate>{rfc822(d)}</pubDate>"
        f"<description>About {t}.</description>{f'<comments>{c[0]}</comments>' if c else ''}</item>"
        for t, l, d, *c in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>{body}</channel></rss>'


def atom(title, entries):
    body = "".join(
        f'<entry><title>{t}</title><link rel="alternate" href="{l}"/><published>{iso(d)}</published>'
        f"<summary>About {t}.</summary></entry>" for t, l, d in entries)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>{title}</title>{body}</feed>'


class FeedServer:
    """Fixture feeds on a local port: no internet needed, same results every run."""

    def __init__(self, routes):
        self.routes = routes  # path -> (status, content_type, body)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, kind, body = outer.routes.get(self.path, (404, "text/plain", "not found"))
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = newspaper.PageServer(("127.0.0.1", 0), Handler)  # skips the slow DNS lookup on macOS
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def opml(feeds):
    lines = "".join(f'    <outline text="{n}" title="{n}" type="rss" xmlUrl="{u}" htmlUrl="{h}"/>\n' for n, u, h in feeds)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<opml version="1.0">\n  <head><title>t</title></head>\n  <body>\n{lines}  </body>\n</opml>\n'
