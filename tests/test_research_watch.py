"""All network responses mocked; artifacts stay in temporary directories."""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "tools/research-watch.py"


class ResearchWatchTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("research_watch_tests_module", SCRIPT)
        self.watch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.watch)
        self.temp = tempfile.TemporaryDirectory(prefix="research-watch-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "inbox"
        self.cfg = self.watch.load_config(SCRIPT.parents[1] / "config/research-watch.json")
        self.cfg["sources"] = self.cfg["sources"][:1]
        self.url = self.cfg["sources"][0]["url"]
        self.now = dt.datetime(2026, 11, 17, 9, 0, tzinfo=self.watch.TAIPEI)

    def response(self, body=b"official A"):
        return {"body": body, "final_url": self.url, "content_type": "text/html",
                "redirect_chain": [{"url": self.url, "http_status": 200}], "http_status": 200}

    def run_watch(self, body=b"official A", write=True, due=False, now=None):
        with mock.patch.object(self.watch, "fetch", return_value=(self.response(body), [])):
            return self.watch.watch(self.cfg, self.root, write=write, due=due, now=now or self.now)

    def test_first_response_is_snapshot_not_new_release(self):
        result = self.run_watch()
        row = result["sources"][0]
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["event"], "snapshot")
        self.assertFalse(row["queued"])
        self.assertEqual(Path(row["snapshot"]).read_bytes(), b"official A")
        self.assertEqual(list((self.root / "items").glob("*.json")), [])
        self.assertEqual(json.loads(Path(result["log"]).read_text())["sources"][0]["event"], "snapshot")

    def test_url_hash_dedup_change_repeat_and_reversion(self):
        self.run_watch()
        changed = self.run_watch(b"official B")
        same = self.run_watch(b"official B")
        reverted = self.run_watch()
        repeat_change = self.run_watch(b"official B")
        self.assertEqual(changed["sources"][0]["event"], "changed")
        self.assertTrue(changed["sources"][0]["queued"])
        self.assertEqual(same["sources"][0]["status"], "nochange")
        self.assertEqual(reverted["sources"][0]["event"], "known_content")
        self.assertEqual(repeat_change["sources"][0]["event"], "known_content")
        items = list((self.root / "items").glob("*.json"))
        self.assertEqual(len(items), 1)
        item = json.loads(items[0].read_text())
        self.assertEqual(item["status"], "candidate")
        self.assertEqual(item["research_decision"], "not_evaluated")
        self.assertEqual(len(list((self.root / "snapshots").rglob("*.bin"))), 2)

    def test_same_content_different_urls_do_not_alias(self):
        second = copy.deepcopy(self.cfg["sources"][0])
        second.update(id="charls-second", url=self.url + "news/")
        self.cfg["sources"].append(second)
        self.run_watch()
        self.run_watch(b"official B")
        items = [json.loads(p.read_text()) for p in (self.root / "items").glob("*.json")]
        self.assertEqual(len(items), 2)
        self.assertEqual({x["identity"]["url"] for x in items}, {self.url, self.url + "news/"})

    def test_error_never_becomes_nochange_and_does_not_advance_week(self):
        self.run_watch()
        previous = json.loads((self.root / "watch-state.json").read_text())
        next_week = self.now + dt.timedelta(days=7)
        with mock.patch.object(self.watch, "fetch", return_value=(None, ["HTTP 503"] * 3)):
            result = self.watch.watch(self.cfg, self.root, write=True, due=True, now=next_week)
        self.assertEqual(result["result"], "error")
        self.assertEqual(result["sources"][0]["status"], "error")
        current = json.loads((self.root / "watch-state.json").read_text())
        self.assertEqual(current["last_week"], previous["last_week"])
        self.assertEqual(current["sources"], previous["sources"])
        self.assertEqual(json.loads(Path(result["log"]).read_text())["sources"][0]["attempts"], 3)

    def test_network_retry_at_most_two_and_policy_violation_no_retry(self):
        with mock.patch.object(self.watch, "fetch_once", side_effect=self.watch.WatchError("offline")) as network:
            result, errors = self.watch.fetch(self.url, self.cfg)
        self.assertIsNone(result)
        self.assertEqual(network.call_count, 3)
        self.assertEqual(len(errors), 3)
        with mock.patch.object(self.watch, "fetch_once", side_effect=self.watch.PolicyError("blocked")) as network:
            result, errors = self.watch.fetch(self.url, self.cfg)
        self.assertIsNone(result)
        self.assertEqual(network.call_count, 1)

    def test_redirect_outside_allowlist_is_never_requested(self):
        for target in ["https://evil.example/", "https://www.isss.pku.edu.cn/other/",
                       "http://charls.pku.edu.cn/", "https://charls.pku.edu.cn.evil.example/"]:
            with self.subTest(target=target), mock.patch.object(self.watch, "curl_once",
                     return_value=(302, {"location": target}, b"redirect")) as network:
                with self.assertRaises(self.watch.PolicyError):
                    self.watch.fetch_once(self.url, self.cfg)
                self.assertEqual(network.call_count, 1)

    def test_allowlist_path_boundary_credentials_and_encoded_traversal(self):
        for url in ["https://www.isss.pku.edu.cn/cfps-evil/", "https://user@charls.pku.edu.cn/",
                    "https://charls.pku.edu.cn:444/", "https://www.isss.pku.edu.cn/cfps/%2e%2e/other",
                    "https://www.isss.pku.edu.cn/cfps/%252e%252e/other", "https://charls.pku.edu.cn/\\other"]:
            with self.subTest(url=url), self.assertRaises(self.watch.PolicyError):
                self.watch.allowed_url(url, self.cfg["allowlist"])
        self.assertEqual(self.watch.allowed_url("https://isss.pku.edu.cn/cfps", self.cfg["allowlist"]),
                         "https://isss.pku.edu.cn/cfps")

    def test_allowed_redirect_chain_and_http_error(self):
        with mock.patch.object(self.watch, "curl_once", side_effect=[
            (302, {"location": "/news/"}, b"redirect"), (200, {"content-type": "text/html"}, b"official")
        ]) as network:
            result = self.watch.fetch_once(self.url, self.cfg)
        self.assertEqual(network.call_args_list[1].args[0], self.url + "news/")
        self.assertEqual(result["final_url"], self.url + "news/")
        self.assertEqual(len(result["redirect_chain"]), 2)
        for code in (304, 404, 503):
            with mock.patch.object(self.watch, "curl_once", return_value=(code, {}, b"error")):
                with self.assertRaisesRegex(self.watch.WatchError, str(code)):
                    self.watch.fetch_once(self.url, self.cfg)

    def test_empty_and_compressed_responses_are_errors(self):
        for body, headers in [(b"", {}), (b"compressed", {"content-encoding": "gzip"})]:
            with mock.patch.object(self.watch, "curl_once", return_value=(200, headers, body)):
                with self.assertRaises(self.watch.WatchError):
                    self.watch.fetch_once(self.url, self.cfg)

    def test_due_uses_taiwan_monday_boundary_and_catches_missed_schedule(self):
        self.cfg["schedule"] = {"weekday": 0, "hour": 8, "minute": 30}
        self.assertEqual(self.watch.due_week(dt.datetime(2026, 11, 16, 0, 29, tzinfo=dt.timezone.utc)), "2026-11-09")
        self.assertEqual(self.watch.due_week(dt.datetime(2026, 11, 16, 0, 30, tzinfo=dt.timezone.utc)), "2026-11-16")
        previous = self.now - dt.timedelta(days=7)
        self.run_watch(due=True, now=previous)
        # 设备周一睡眠、周三才运行，仍检查本周应执行的轮次。
        wednesday = dt.datetime(2026, 11, 18, 15, 0, tzinfo=self.watch.TAIPEI)
        with mock.patch.object(self.watch, "fetch", return_value=(self.response(), [])) as network:
            result = self.watch.watch(self.cfg, self.root, write=True, due=True, now=wednesday)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(result["due_week"], "2026-11-16")
        with mock.patch.object(self.watch, "fetch") as network:
            skipped = self.watch.watch(self.cfg, self.root, write=True, due=True, now=wednesday)
        network.assert_not_called()
        self.assertEqual(skipped["result"], "skipped")

    def test_daily_10am_schedule_dedup_and_missed_catchup(self):
        self.assertEqual(self.cfg["schedule"], {"cadence": "daily", "hour": 10, "minute": 0})
        t_yesterday = dt.datetime(2026, 11, 17, 10, 5, tzinfo=self.watch.TAIPEI)
        self.run_watch(due=True, now=t_yesterday)
        # 次日 09:59 尚未到 10:00，归属前一日周期，跳过
        before_10 = dt.datetime(2026, 11, 18, 9, 59, tzinfo=self.watch.TAIPEI)
        with mock.patch.object(self.watch, "fetch") as network:
            early = self.watch.watch(self.cfg, self.root, write=True, due=True, now=before_10)
        network.assert_not_called()
        self.assertEqual(early["result"], "skipped")
        # 次日 10:00 后触发当日检查，同日再次触发跳过
        after_10 = dt.datetime(2026, 11, 18, 10, 0, tzinfo=self.watch.TAIPEI)
        with mock.patch.object(self.watch, "fetch", return_value=(self.response(), [])) as network:
            ran = self.watch.watch(self.cfg, self.root, write=True, due=True, now=after_10)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(ran["due_day"], "2026-11-18")
        with mock.patch.object(self.watch, "fetch") as network:
            dup = self.watch.watch(self.cfg, self.root, write=True, due=True, now=after_10 + dt.timedelta(hours=2))
        network.assert_not_called()
        self.assertEqual(dup["result"], "skipped")

    def test_new_source_requires_check_even_if_week_was_complete(self):
        self.run_watch(due=True)
        second = copy.deepcopy(self.cfg["sources"][0])
        second.update(id="charls-second", url=self.url + "news/")
        self.cfg["sources"].append(second)
        with mock.patch.object(self.watch, "fetch", side_effect=lambda *a: (self.response(), [])) as network:
            result = self.watch.watch(self.cfg, self.root, write=True, due=True, now=self.now)
        self.assertEqual(network.call_count, 2)
        self.assertEqual(result["sources"][1]["event"], "snapshot")

    def test_dry_run_does_not_create_inbox(self):
        result = self.run_watch(write=False)
        self.assertFalse(self.root.exists())
        self.assertEqual(result["sources"][0]["event"], "snapshot")

    def test_enqueue_preserves_evidence_and_deduplicates(self):
        evidence = Path(self.temp.name) / "report.md"
        evidence.write_text("proof, not a research decision")
        args = dict(project="博士论文", source="claude-session-example", evidence=[evidence],
                    status="confirmed_evidence", write=True, now=self.now)
        first = self.watch.enqueue(self.root, **args)
        second = self.watch.enqueue(self.root, **args)
        self.assertEqual(first["result"], "queued")
        self.assertEqual(second["result"], "duplicate")
        item = json.loads(Path(first["queue_item"]).read_text())
        self.assertEqual(item["status"], "confirmed_evidence")
        self.assertEqual(item["research_decision"], "not_evaluated")
        self.assertEqual(item["evidence"][0]["sha256"], self.watch.sha(evidence.read_bytes()))
        self.assertEqual(evidence.read_text(), "proof, not a research decision")

    def test_enqueue_preview_and_missing_evidence_fail_closed(self):
        evidence = Path(self.temp.name) / "report.md"
        evidence.write_text("proof")
        result = self.watch.enqueue(self.root, project="test", source="local task", evidence=[evidence],
                                    status="candidate", write=False)
        self.assertEqual(result["result"], "preview")
        self.assertFalse(self.root.exists())
        with self.assertRaises(self.watch.WatchError):
            self.watch.enqueue(self.root, project="test", source="local task", evidence=[evidence.with_suffix(".missing")],
                               status="confirmed_evidence", write=True)
        self.assertFalse(self.root.exists())

    def test_corrupted_snapshot_is_preserved_and_fails(self):
        first = self.run_watch()
        snapshot = Path(first["sources"][0]["snapshot"])
        snapshot.write_bytes(b"human edit")
        with self.assertRaisesRegex(self.watch.WatchError, "快照被修改"):
            self.run_watch()
        self.assertEqual(snapshot.read_bytes(), b"human edit")

    def test_launchd_daily_calendar_and_due_entry_no_bootstrap(self):
        path = SCRIPT.parent / "launchd/com.example.research-vault-watch.plist"
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 10, "Minute": 0})
        self.assertIn("--due", data["ProgramArguments"])
        self.assertIn("--write", data["ProgramArguments"])
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["EnvironmentVariables"]["TZ"], "Asia/Taipei")

    def test_watch_and_enqueue_share_nonblocking_lock(self):
        evidence = Path(self.temp.name) / "proof.md"
        evidence.write_text("proof")
        with self.watch.inbox_lock(self.root, True):
            with self.assertRaisesRegex(self.watch.WatchError, "正在运行"):
                self.watch.enqueue(self.root, project="test", source="task", evidence=[evidence],
                                   status="candidate", write=True)
            with mock.patch.object(self.watch, "fetch") as network:
                with self.assertRaisesRegex(self.watch.WatchError, "正在运行"):
                    self.watch.watch(self.cfg, self.root, write=True, due=False, now=self.now)
            network.assert_not_called()

    def test_config_cannot_expand_official_roots_or_retry_limit(self):
        for changed in [{"allowlist": ["https://evil.example/"]}, {"retries": 3}]:
            cfg = copy.deepcopy(self.cfg)
            cfg.update(changed)
            path = Path(self.temp.name) / "config.json"
            path.write_text(json.dumps(cfg))
            with self.assertRaises(self.watch.WatchError):
                self.watch.load_config(path)

    def test_curl_stream_byte_bound_and_process_deadline(self):
        real_popen = subprocess.Popen
        def fake_curl(argv, **kwargs):
            return real_popen([sys.executable, "-B", "-c", "import sys; sys.stdout.write('x'*10000)"], **kwargs)
        with mock.patch.object(self.watch.subprocess, "Popen", side_effect=fake_curl):
            with self.assertRaisesRegex(self.watch.WatchError, "大小上限"):
                self.watch.curl_once(self.url, 2, 100)
        def slow_curl(argv, **kwargs):
            return real_popen([sys.executable, "-B", "-c", "import time; time.sleep(10)"], **kwargs)
        with mock.patch.object(self.watch.subprocess, "Popen", side_effect=slow_curl):
            with self.assertRaisesRegex(self.watch.WatchError, "总时限"):
                self.watch.curl_once(self.url, 0.1, 100)


if __name__ == "__main__":
    unittest.main()
