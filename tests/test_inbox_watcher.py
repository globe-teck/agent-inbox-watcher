"""Behavior tests for inbox_watcher.py. Run: python3 -m unittest discover -s tests"""
import datetime as dt
import http.server
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TZ = ZoneInfo("America/New_York")

import logging
logging.getLogger("inbox_watcher").addHandler(logging.NullHandler())


class _Hook(http.server.BaseHTTPRequestHandler):
    calls = []
    status = 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        _Hook.calls.append({"auth": self.headers.get("Authorization"),
                            "body": json.loads(self.rfile.read(n))})
        self.send_response(_Hook.status)
        self.end_headers()

    def log_message(self, *a):
        pass


def local_epoch(y, mo, d, h, mi=0, s=0):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp()


# Monday 2030-01-07 is a weekday.
MON_10AM = local_epoch(2030, 1, 7, 10)


class WatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Hook)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.inbox)
        os.makedirs(self.state)
        self.env = {"AIW_INBOX": self.inbox, "AIW_STATE_DIR": self.state, "AIW_TZ": "America/New_York",
                    "WAKE_URL": f"http://127.0.0.1:{self.server.server_port}/wake",
                    "WAKE_AUTH": "Bearer example-not-a-real-token"}
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        import inbox_watcher
        self.w = importlib.reload(inbox_watcher)
        self.w.ALLOW_INSECURE_HTTP = True  # local test server is plain http
        _Hook.calls.clear()
        _Hook.status = 200

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def drop(self, name, urgency, at):
        p = os.path.join(self.inbox, name)
        with open(p, "w") as f:
            f.write(f"URGENCY: {urgency}\n\n# example\n" if urgency else "# no urgency line\n")
        os.utime(p, (at, at))
        return p

    def acks(self):
        try:
            with open(os.path.join(self.state, "acks.jsonl")) as f:
                return [json.loads(l) for l in f]
        except FileNotFoundError:
            return []

    def test_risk_wakes_on_next_pass(self):
        self.drop("a-risk.md", "risk", MON_10AM)
        self.w.run_pass(MON_10AM + 3)
        self.assertEqual(len(_Hook.calls), 1)
        self.assertEqual(_Hook.calls[0]["body"], {"reason": "risk", "files": ["a-risk.md"], "count": 1})
        self.assertEqual(_Hook.calls[0]["auth"], "Bearer example-not-a-real-token")
        self.assertTrue(self.acks()[0]["ok"])

    def test_file_still_being_written_is_skipped(self):
        self.drop("a-risk.md", "risk", MON_10AM)
        self.w.run_pass(MON_10AM + 0.5)
        self.assertEqual(_Hook.calls, [])

    def test_announced_once_only(self):
        self.drop("a-risk.md", "risk", MON_10AM)
        self.w.run_pass(MON_10AM + 3)
        self.w.run_pass(MON_10AM + 5)
        self.assertEqual(len(_Hook.calls), 1)

    def test_decisions_close_together_share_one_wake(self):
        self.drop("d1.md", "decision", MON_10AM)
        self.drop("d2.md", "decision", MON_10AM + 1)
        self.w.run_pass(MON_10AM + 2.5)          # inside the grouping wait
        self.assertEqual(_Hook.calls, [])
        self.w.run_pass(MON_10AM + 4)
        self.assertEqual(len(_Hook.calls), 1)
        self.assertEqual(_Hook.calls[0]["body"]["files"], ["d1.md", "d2.md"])

    def test_decision_wake_within_max_hold(self):
        self.drop("d1.md", "decision", MON_10AM)
        self.w.run_pass(MON_10AM + self.w.DECISION_MAX_HOLD)
        self.assertEqual(len(_Hook.calls), 1)

    def test_fyi_batched_with_min_gap(self):
        self.drop("f1.md", "fyi", MON_10AM)
        self.w.run_pass(MON_10AM + 5)
        self.drop("f2.md", "fyi", MON_10AM + 60)
        self.w.run_pass(MON_10AM + 70)           # under 2 h: held
        self.assertEqual(len(_Hook.calls), 1)
        self.w.run_pass(MON_10AM + 2 * 3600 + 5)
        self.assertEqual(len(_Hook.calls), 2)
        self.assertEqual(_Hook.calls[1]["body"]["files"], ["f2.md"])

    def test_missing_urgency_counts_as_fyi(self):
        self.drop("x.md", None, MON_10AM)
        self.w.run_pass(MON_10AM + 5)
        self.assertEqual(_Hook.calls[0]["body"]["reason"], "fyi-batch")

    def test_fyi_held_outside_window_but_risk_still_wakes(self):
        sat = local_epoch(2030, 1, 5, 11)
        self.drop("f.md", "fyi", sat)
        self.drop("r.md", "risk", sat)
        self.w.run_pass(sat + 5)
        self.assertEqual([c["body"]["reason"] for c in _Hook.calls], ["risk"])

    def test_end_of_window_flush_ignores_gap_once(self):
        t1 = local_epoch(2030, 1, 7, 15, 30)
        self.drop("f1.md", "fyi", t1)
        self.w.run_pass(t1 + 5)
        t2 = local_epoch(2030, 1, 7, 16, 50)     # in the last 15 min, under 2 h since 15:30
        self.drop("f2.md", "fyi", t2 - 60)
        self.w.run_pass(t2)
        self.assertEqual(len(_Hook.calls), 2)
        self.drop("f3.md", "fyi", t2 + 60)
        self.w.run_pass(t2 + 120)                # flush already used today
        self.assertEqual(len(_Hook.calls), 2)

    def test_failure_backs_off_and_is_not_announced(self):
        _Hook.status = 401
        self.drop("r.md", "risk", MON_10AM)
        self.w.run_pass(MON_10AM + 3)
        self.w.run_pass(MON_10AM + 10)           # inside backoff: no retry
        self.assertEqual(len(_Hook.calls), 1)
        self.assertFalse(self.acks()[0]["ok"])
        _Hook.status = 200
        self.w.run_pass(MON_10AM + 3 + self.w.BACKOFF_START)
        self.assertEqual(len(_Hook.calls), 2)
        self.assertTrue(self.acks()[-1]["ok"])

    def test_dry_run_without_credentials(self):
        os.environ.pop("WAKE_URL")
        self.drop("r.md", "risk", MON_10AM)
        r = self.w.run_pass(MON_10AM + 3)
        self.assertEqual(_Hook.calls, [])
        self.assertIn("r.md", r["pending"])
        self.assertEqual(self.acks()[0]["mode"], "dry-run")

    def test_moved_files_are_forgotten(self):
        p = self.drop("r.md", "risk", MON_10AM)
        self.w.run_pass(MON_10AM + 3)
        os.remove(p)
        self.w.run_pass(MON_10AM + 5)
        self.assertEqual(self.w.load_state()["announced"], [])

    def test_secrets_are_redacted(self):
        msg = self.w.redact(f"failed {os.environ['WAKE_URL']} with example-not-a-real-token")
        self.assertNotIn("127.0.0.1", msg)
        self.assertNotIn("example-not-a-real-token", msg)


    def test_plain_http_rejected_without_flag(self):
        self.w.ALLOW_INSECURE_HTTP = False
        self.drop("2030-01-07-1000-me-x.md", "risk", MON_10AM)
        self.w.run_pass(MON_10AM + 5)
        with open(self.w.ACK_PATH) as f:
            self.assertIn("insecure-url", f.read())
        self.assertEqual(_Hook.calls, [])

    def test_receipt_and_pid_files_are_0600(self):
        self.w.write_acks("risk", ["a.md"], True, 200, "live")
        self.assertEqual(os.stat(self.w.ACK_PATH).st_mode & 0o777, 0o600)
        self.assertTrue(self.w.acquire_lock())
        self.assertEqual(os.stat(self.w.PID_PATH).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
