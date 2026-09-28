import unittest

from app.services.inspection.urlsafety import (
    hosts_match,
    redact_secrets,
    safe_model_id,
    sanitize_http_url,
)


class UrlSafetyTests(unittest.TestCase):
    def test_adds_http_scheme(self):
        self.assertEqual(sanitize_http_url("prometheus:9090"), "http://prometheus:9090")

    def test_rejects_embedded_credentials(self):
        with self.assertRaises(ValueError):
            sanitize_http_url("http://user:pass@evil.example/path")

    def test_uppercase_scheme_is_normalized(self):
        self.assertEqual(sanitize_http_url("HTTPS://prom:9090"), "https://prom:9090")
        self.assertEqual(sanitize_http_url("HTTP://prom:9090"), "http://prom:9090")
        self.assertTrue(hosts_match("HTTPS://prom:9090", "http://prom:9090"))

    def test_rejects_non_http(self):
        with self.assertRaises(ValueError):
            sanitize_http_url("file:///etc/passwd")

    def test_hosts_match_ignores_path(self):
        self.assertTrue(hosts_match("http://prom:9090/api/v1/query", "http://prom:9090"))
        self.assertFalse(hosts_match("http://evil.example", "http://prom:9090"))

    def test_hosts_match_requires_same_port(self):
        self.assertFalse(hosts_match("http://prom:8080", "http://prom:9090"))
        self.assertTrue(hosts_match("https://prom", "https://prom:443"))
        self.assertTrue(hosts_match("http://prom", "http://prom:80"))

    def test_same_host_port_ignores_scheme(self):
        # 配置里常写 prom:9090（补成 http），环境可能是 https://prom:9090
        self.assertTrue(hosts_match("http://prom:9090", "https://prom:9090"))
        self.assertFalse(hosts_match("http://api.openai.com", "https://api.openai.com"))

    def test_safe_model_id(self):
        self.assertEqual(safe_model_id("gpt-4o-mini"), "gpt-4o-mini")
        self.assertEqual(safe_model_id("foo bar", fallback="gpt-4o-mini"), "gpt-4o-mini")
        self.assertEqual(safe_model_id("../evil", fallback="gpt-4o-mini"), "gpt-4o-mini")

    def test_redact_secrets(self):
        text = redact_secrets("key=abcd1234 Bearer tok_secret")
        self.assertNotIn("abcd1234", text)
        self.assertNotIn("tok_secret", text)
