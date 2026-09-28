"""模拟测试人员对日志监控 / 巡检关键路径的回归。不依赖 DB / FastAPI。"""
from __future__ import annotations

import ast
import datetime
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.inspection.urlsafety import (
    hosts_match,
    redact_secrets,
    safe_model_id,
    sanitize_http_url,
)
from app.services.log_monitor.log_keyword_match import keyword_matches_line

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_APP = Path(__file__).resolve().parents[2]


def _load_fns(rel: str, names: set[str], extras: dict):
    tree = ast.parse((_APP / rel).read_text(encoding="utf-8"))
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    future = ast.parse("from __future__ import annotations").body
    mod = ast.Module(body=future + body, type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = dict(extras)
    exec(compile(mod, rel, "exec"), ns)  # noqa: S102
    return ns


_MON = _load_fns("api/monitor.py", {"_safe_local_log_path", "_download_name", "_keyword_terms"}, {"os": os, "re": re})
_safe_local_log_path = _MON["_safe_local_log_path"]
_download_name = _MON["_download_name"]
_keyword_terms = _MON["_keyword_terms"]

_S3 = _load_fns(
    "services/log_monitor/s3_helpers.py",
    {"task_s3_prefixes", "index_has_files", "virtual_raw_log_name"},
    {"re": re, "MonitorTask": object},
)
task_s3_prefixes = _S3["task_s3_prefixes"]
index_has_files = _S3["index_has_files"]
virtual_raw_log_name = _S3["virtual_raw_log_name"]

_REPORT_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def text_to_lines(v: str):
    return [s.strip() for s in v.split("\n") if s.strip()]


def to_local_input(d: datetime.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def make_aware(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=_SHANGHAI)


def strip_secrets_payload(payload: dict) -> dict:
    """对应 logs/page.tsx saveTask 的脱敏字段剥离。"""
    data = dict(payload)
    for k in (
        "id", "last_run", "last_error", "alerts_sent_count", "alert_state",
        "threshold_state", "created_at", "updated_at", "slack_webhook_set",
        "k8s_kubeconfig_set", "s3_secret_key_set",
    ):
        data.pop(k, None)
    if not data.get("k8s_kubeconfig"):
        data.pop("k8s_kubeconfig", None)
    if not data.get("s3_secret_key"):
        data.pop("s3_secret_key", None)
    ak = data.get("s3_access_key")
    if isinstance(ak, str) and ak.startswith("****"):
        data.pop("s3_access_key", None)
    wh = data.get("slack_webhook_url")
    if not wh or (isinstance(wh, str) and wh.startswith("•")):
        data.pop("slack_webhook_url", None)
    return data


def ai_target(config, platform_base: str, platform_key: str, platform_model: str):
    try:
        custom_base = sanitize_http_url(config.ark_base_url)
    except ValueError:
        custom_base = ""
    custom_key = (config.ark_api_key or "").strip()
    custom_model = (config.ark_model_id or "").strip()
    try:
        pbase = sanitize_http_url(platform_base)
    except ValueError:
        pbase = ""
    if custom_base and not hosts_match(custom_base, pbase):
        return custom_base, custom_key, custom_model
    return pbase or custom_base, custom_key or platform_key, custom_model or platform_model


class PathAndDownloadTests(unittest.TestCase):
    def test_local_path_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "nested"), exist_ok=True)
            good = os.path.join(td, "app.log")
            Path(good).write_text("ok", encoding="utf-8")
            self.assertTrue(_safe_local_log_path(td, "app.log"))
            self.assertIsNone(_safe_local_log_path(td, "../app.log"))
            self.assertIsNone(_safe_local_log_path(td, "nested/../app.log"))
            self.assertIsNone(_safe_local_log_path(td, "app.log/../../etc/passwd"))
            self.assertIsNone(_safe_local_log_path(td, ""))
            dotted = os.path.join(td, "backup..log")
            Path(dotted).write_text("ok", encoding="utf-8")
            self.assertTrue(_safe_local_log_path(td, "backup..log"))

    def test_download_name_strips_header_injection(self):
        name = _download_name('evil\r\nContent-Type: text/html"x.log')
        self.assertNotIn("\r", name)
        self.assertNotIn("\n", name)
        self.assertNotIn('"', name)


class S3PrefixTests(unittest.TestCase):
    def test_monitor_name_cannot_read_all_tasks(self):
        tid = "11111111-1111-1111-1111-111111111111"
        p = task_s3_prefixes(SimpleNamespace(id=tid, name="monitor"))
        self.assertEqual(p, [f"logs/monitor/{tid}/"])

    def test_slash_in_name_rejected(self):
        tid = "22222222-2222-2222-2222-222222222222"
        p = task_s3_prefixes(SimpleNamespace(id=tid, name=f"monitor/{tid}"))
        self.assertEqual(p, [f"logs/monitor/{tid}/"])

    def test_safe_name_keeps_legacy_prefix(self):
        tid = "33333333-3333-3333-3333-333333333333"
        p = task_s3_prefixes(SimpleNamespace(id=tid, name="app-logs"))
        self.assertEqual(p[0], f"logs/monitor/{tid}/")
        self.assertIn("logs/app-logs/", p)

    def test_empty_realtime_index_falls_back(self):
        self.assertFalse(index_has_files(None))
        self.assertFalse(index_has_files({"files": []}))
        self.assertFalse(index_has_files({"files": None}))
        self.assertTrue(index_has_files({"files": [{"name": "x"}]}))

    def test_virtual_raw_name_and_error_keys_not_aggregated(self):
        tid = "44444444-4444-4444-4444-444444444444"
        raw = f"logs/monitor/{tid}/raw/ns/pod-a/2026-09-28/00.log"
        err = f"logs/monitor/{tid}/error/ns/pod-a/2026-09-28/00.log"
        self.assertEqual(virtual_raw_log_name(raw), "ns_pod-a_s3_recent.log")
        self.assertIsNone(virtual_raw_log_name(err))
        self.assertIsNone(virtual_raw_log_name("not-an-s3-key.log"))
        self.assertIsNone(virtual_raw_log_name(f"logs/monitor/{tid}/raw/ns/only-two"))


class SavePayloadTests(unittest.TestCase):
    def test_edit_save_does_not_wipe_webhook(self):
        payload = {
            "id": "x",
            "name": "t1",
            "slack_webhook_url": "",
            "slack_webhook_set": True,
            "k8s_kubeconfig": "",
            "s3_secret_key": "",
            "s3_access_key": "****abcd",
        }
        out = strip_secrets_payload(payload)
        self.assertNotIn("slack_webhook_url", out)
        self.assertNotIn("k8s_kubeconfig", out)
        self.assertNotIn("s3_secret_key", out)
        self.assertNotIn("s3_access_key", out)

    def test_new_webhook_is_kept(self):
        payload = {"slack_webhook_url": "https://hooks.slack.com/services/AAA/BBB/CCC"}
        out = strip_secrets_payload(payload)
        self.assertEqual(out["slack_webhook_url"], payload["slack_webhook_url"])

    def test_keyword_lines_roundtrip(self):
        raw = "error\n\n exception \n"
        self.assertEqual(text_to_lines(raw), ["error", "exception"])

    def test_blank_keyword_does_not_match_all_lines(self):
        self.assertIsNone(_keyword_terms(None))
        self.assertIsNone(_keyword_terms(""))
        self.assertIsNone(_keyword_terms("   \t"))
        self.assertEqual(_keyword_terms("error  boom"), ["error", "boom"])
        # all([]) 在 Python 里为 True，空白关键字必须是 None 而不是 []
        self.assertIsNone(_keyword_terms("   "))


class HistoryTimezoneTests(unittest.TestCase):
    def test_datetime_local_treated_as_shanghai(self):
        local = datetime.datetime(2026, 9, 28, 15, 30)
        sent = to_local_input(local)
        parsed = datetime.datetime.fromisoformat(sent)
        aware = make_aware(parsed)
        self.assertEqual(aware.tzinfo, _SHANGHAI)
        self.assertEqual(aware.hour, 15)


class InspectionAiTests(unittest.TestCase):
    def test_empty_custom_uses_platform_key(self):
        cfg = SimpleNamespace(ark_base_url="", ark_api_key="", ark_model_id="")
        base, key, model = ai_target(cfg, "https://api.openai.com/v1", "sk-platform", "gpt-4o")
        self.assertEqual(key, "sk-platform")
        self.assertTrue(hosts_match(base, "https://api.openai.com/v1"))
        self.assertEqual(model, "gpt-4o")

    def test_external_gateway_does_not_inherit_platform_key(self):
        cfg = SimpleNamespace(ark_base_url="https://evil.example/v1", ark_api_key="", ark_model_id="x")
        base, key, model = ai_target(cfg, "https://api.openai.com/v1", "sk-platform", "gpt-4o")
        self.assertTrue(hosts_match(base, "https://evil.example"))
        self.assertEqual(key, "")
        self.assertEqual(model, "x")

    def test_same_host_as_platform_still_gets_key(self):
        cfg = SimpleNamespace(
            ark_base_url="https://api.openai.com/v1",
            ark_api_key="",
            ark_model_id="",
        )
        _base, key, _model = ai_target(cfg, "https://api.openai.com/v1", "sk-platform", "gpt-4o")
        self.assertEqual(key, "sk-platform")

    def test_prom_auth_not_sent_to_other_port(self):
        self.assertFalse(hosts_match("http://prom:8080/api/v1/query", "http://prom:9090"))
        self.assertTrue(hosts_match("http://prom:9090", "https://prom:9090"))

    def test_report_id_validation(self):
        self.assertTrue(_REPORT_ID_RE.match("2026-09-28"))
        self.assertFalse(_REPORT_ID_RE.match("../2026-09-28"))
        self.assertFalse(_REPORT_ID_RE.match("2026-9-28"))
        self.assertFalse(_REPORT_ID_RE.match("latest"))


class KeywordAndSecretTests(unittest.TestCase):
    def test_access_log_error_path_is_not_alert(self):
        line = '1.1.1.1 - - [28/Sep/2026] "GET /error HTTP/1.1" 200 12'
        self.assertFalse(keyword_matches_line("error", line))

    def test_real_error_still_matches(self):
        self.assertTrue(keyword_matches_line("error", "ERROR: boom exception: x"))

    def test_redact_does_not_leave_key(self):
        out = redact_secrets("Gemini failed key=abcd1234 Bearer tok_secret extra")
        self.assertNotIn("abcd1234", out)
        self.assertNotIn("tok_secret", out)

    def test_model_rejects_path_escape(self):
        self.assertEqual(safe_model_id("../x", fallback="gpt-4o-mini"), "gpt-4o-mini")
        self.assertEqual(sanitize_http_url("prometheus:9090"), "http://prometheus:9090")
        with self.assertRaises(ValueError):
            sanitize_http_url("file:///etc/passwd")


class CrossDirectionTests(unittest.TestCase):
    """安全 / 保存 / 时区 / AI / 告警 五个方向交叉。"""

    def test_security_direction(self):
        with tempfile.TemporaryDirectory() as td:
            Path(os.path.join(td, "a.log")).write_text("x", encoding="utf-8")
            self.assertIsNone(_safe_local_log_path(td, "../a.log"))
        self.assertNotIn("\n", _download_name("a\r\nb.log"))
        tid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        self.assertEqual(task_s3_prefixes(SimpleNamespace(id=tid, name="monitor")), [f"logs/monitor/{tid}/"])

    def test_save_direction(self):
        out = strip_secrets_payload({
            "slack_webhook_url": "",
            "slack_webhook_set": True,
            "s3_access_key": "****1234",
        })
        self.assertNotIn("slack_webhook_url", out)
        kept = strip_secrets_payload({"slack_webhook_url": "https://hooks.slack.com/x"})
        self.assertIn("slack_webhook_url", kept)

    def test_timezone_direction(self):
        sent = to_local_input(datetime.datetime(2026, 9, 28, 8, 0))
        self.assertEqual(sent, "2026-09-28T08:00")
        self.assertEqual(make_aware(datetime.datetime.fromisoformat(sent)).tzinfo, _SHANGHAI)

    def test_ai_direction(self):
        plat = "https://ark.cn-beijing.volces.com/api/v3"
        empty = SimpleNamespace(ark_base_url="", ark_api_key="", ark_model_id="")
        _b, key, _m = ai_target(empty, plat, "sk-platform", "doubao")
        self.assertEqual(key, "sk-platform")
        evil = SimpleNamespace(ark_base_url="http://127.0.0.1:9", ark_api_key="", ark_model_id="m")
        _b, key, _m = ai_target(evil, plat, "sk-platform", "doubao")
        self.assertEqual(key, "")

    def test_alert_direction(self):
        access = '10.0.0.1 - - [28/Sep/2026] "GET /api/error HTTP/1.1" 200 1'
        self.assertFalse(keyword_matches_line("error", access))
        self.assertTrue(keyword_matches_line("error", "[ERROR] disk full"))

    """同一组用例连跑 10 遍，模拟测试人员反复点保存 / 下载 / 巡检配置。"""

    def test_twenty_repetitions_stay_stable(self):
        tid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        for i in range(20):
            with self.subTest(round=i + 1):
                payload = strip_secrets_payload({
                    "name": f"task-{i}",
                    "slack_webhook_url": "",
                    "slack_webhook_set": True,
                    "s3_access_key": "****zzzz",
                    "alert_keywords": text_to_lines("error\nexception\n"),
                })
                self.assertNotIn("slack_webhook_url", payload)
                self.assertEqual(payload["alert_keywords"], ["error", "exception"])

                prefixes = task_s3_prefixes(SimpleNamespace(id=tid, name="monitor"))
                self.assertEqual(len(prefixes), 1)

                fname = _download_name(f"pod_{i}\n.log")
                self.assertNotIn("\n", fname)

                with tempfile.TemporaryDirectory() as td:
                    Path(os.path.join(td, "ok.log")).write_text("x", encoding="utf-8")
                    self.assertIsNone(_safe_local_log_path(td, "../ok.log"))
                    self.assertTrue(_safe_local_log_path(td, "ok.log"))

                cfg = SimpleNamespace(ark_base_url="https://attacker.test", ark_api_key="", ark_model_id="m")
                _b, key, _m = ai_target(cfg, "https://api.openai.com/v1", "sk-live", "gpt")
                self.assertEqual(key, "")
                self.assertFalse(hosts_match("http://prom:8080", "http://prom:9090"))
                self.assertTrue(hosts_match("http://prom:9090", "https://prom:9090"))
                self.assertFalse(hosts_match("http://api.openai.com", "https://api.openai.com"))
                self.assertTrue(_REPORT_ID_RE.match("2026-09-28"))
                self.assertEqual(safe_model_id("../evil", fallback="ok-model"), "ok-model")
                self.assertFalse(index_has_files({"files": []}))
                self.assertEqual(virtual_raw_log_name(f"logs/monitor/{tid}/raw/ns/p/2026/x.log"), "ns_p_s3_recent.log")
                self.assertIsNone(_keyword_terms("  "))
                self.assertEqual(sanitize_http_url("HTTPS://prom:9090"), "https://prom:9090")


class PersistenceGuardTests(unittest.TestCase):
    def test_success_loop_does_not_save_alert_count(self):
        tree = ast.parse((_APP / "services/log_monitor/engine.py").read_text(encoding="utf-8"))
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "attr", "") != "save_task":
                continue
            if len(node.args) < 2 or not isinstance(node.args[1], ast.List):
                continue
            fields = [elt.value for elt in node.args[1].elts if isinstance(elt, ast.Constant)]
            if "last_run" in fields:
                found = True
                self.assertNotIn("alerts_sent_count", fields)
        self.assertTrue(found)

    def test_bootstrap_does_not_reset_alert_settings(self):
        src = (_APP / "bootstrap.py").read_text(encoding="utf-8")
        self.assertNotIn("alert_threshold_count = 1 WHERE", src)
        self.assertNotIn("alert_silence_minutes = 15 WHERE", src)
        self.assertNotIn("alert_state = '{}'", src)

    def test_slack_merge_uses_imported_clean_log_line(self):
        src = (_APP / "services/log_monitor/engine.py").read_text(encoding="utf-8")
        self.assertIn("key = clean_log_line(stripped)", src)
        self.assertNotIn("key = _clean_log_line(", src)

    def test_record_only_does_not_substring_drop_other_alerts(self):
        src = (_APP / "services/log_monitor/engine.py").read_text(encoding="utf-8")
        self.assertNotIn("a['msg'].find(line)", src)
        self.assertIn("alerts = alerts[:alerts_before_line]", src)

    def test_index_upload_failure_keeps_memory(self):
        src = (_APP / "services/log_monitor/engine.py").read_text(encoding="utf-8")
        self.assertIn("if not (raw_ok and err_ok):", src)
        self.assertIn("return False", src)

    def test_pod_log_read_failure_is_not_silent_success(self):
        src = (_APP / "services/log_monitor/engine.py").read_text(encoding="utf-8")
        self.assertIn("log_errors.append(err)", src)
        self.assertIn("fetch_errors = ns_errors + log_errors", src)

    def test_local_rotate_does_not_delete_before_retention_without_s3(self):
        src = (_APP / "services/log_monitor/engine.py").read_text(encoding="utf-8")
        self.assertIn("if not s3_client and file_date > retention_date:", src)
