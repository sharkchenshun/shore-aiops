# ============================================================
# 巡检出站 URL 校验：限制 http(s)、禁止 URL 内嵌凭据。
# ============================================================

from __future__ import annotations

import re
from urllib.parse import urlparse

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def sanitize_http_url(raw: str | None) -> str:
    url = (raw or "").strip()
    if not url:
        return ""
    lowered = url.split(":", 1)[0].lower()
    if lowered in {"file", "javascript", "data", "gopher", "ftp", "unix", "mailto"}:
        raise ValueError("仅支持 http/https URL")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https URL")
    if not parsed.hostname:
        raise ValueError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("不要在 URL 中内嵌用户名/密码")
    return url.rstrip("/")


def url_host(raw: str | None) -> str:
    try:
        url = sanitize_http_url(raw)
    except ValueError:
        return ""
    if not url:
        return ""
    return (urlparse(url).hostname or "").lower()


def _url_authority(raw: str | None) -> str:
    """hostname:port，缺省端口按 http=80 / https=443。凭据绑定必须连端口一起比。"""
    try:
        url = sanitize_http_url(raw)
    except ValueError:
        return ""
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}:{port}"


def hosts_match(left: str | None, right: str | None) -> bool:
    a, b = _url_authority(left), _url_authority(right)
    return bool(a and b and a == b)


def safe_model_id(raw: str | None, fallback: str = "") -> str:
    model = (raw or "").strip() or fallback
    if not model or ".." in model or model.startswith("/") or not _MODEL_RE.match(model):
        return fallback if fallback and ".." not in fallback and _MODEL_RE.match(fallback) else ""
    return model


def redact_secrets(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"(key=)[^&\s]+", r"\1***", text, flags=re.I)
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", text, flags=re.I)
    return text[:500]
