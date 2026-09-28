# ============================================================
# log_keyword_match.py — 日志关键字智能匹配（减少误报）
# ============================================================

from __future__ import annotations

import re

# DEBUG/配置行：SuppressFatalErrorMessage = false {product}
_DEBUG_CONFIG = re.compile(
    r"(=\s*(false|true|null)\b|\{product\}|\{default\}|^\s*bool\s+\w+\s*=)",
    re.I,
)
# SQL / 语句中的 error 子串（非真实错误）
_SQL_ERROR = re.compile(
    r"\b(on\s+error|without\s+error|no\s+error|ignore\s+error|handle\s+error|"
    r"error\s*=\s*|error_code|error_msg|is_error|has_error|check_error)\b",
    re.I,
)
# 真实异常/错误信号
_REAL_EXCEPTION = re.compile(
    r"\b\w+Exception\b|\b\w+Error\b.*(?:exception|:)|"
    r"\bexception\s*:|caused by\s*:|"
    r"throw\w*\s+\w*(?:Exception|Error)\b|"
    r"nested exception is ",
    re.I,
)
_REAL_ERROR = re.compile(
    r"\b(ERROR|FATAL)\b|"
    r"\berror\s*:|"  # error: message
    r"\bfailed\b|\bfailure\b|\bfatal\b|\bpanic\b|"
    r"uncaught\s+exception|"
    r"completionexception|invocationtargetexception",
    re.I,
)
# camelCase 内嵌 error（SuppressFatalErrorMessage）但无独立 error 词
_EMBEDDED_ERROR = re.compile(r"[a-z]error[a-z]", re.I)
# 限流/业务配置行（非错误）
_CONFIG_NOISE = re.compile(
    r"(单接口=|\d+次/\d+s|^\s*配置\b|配置\s+\S+\s*=|"
    r"rate\s*limit|qps\s*=|threshold\s*=|"
    r"errorlimit|errorhandler|errorrate|suppress.*error|"
    r"^\s*bool\s+\w+|=\s*(false|true|null)\b|\{product\}|\{default\})",
    re.I,
)
_PIPE_LEVEL = re.compile(
    r"\|\s*(ERROR|WARN|INFO|DEBUG|TRACE|FATAL)\s+\d+\s*\|",
    re.I,
)
_BRACKET_LEVEL = re.compile(
    r"^\s*\[(ERROR|WARN|INFO|DEBUG|TRACE|FATAL)\]\b",
    re.I,
)
# Nginx/Apache 访问日志："GET /path HTTP/1.1" 200 ...
_ACCESS_LOG = re.compile(
    r'"\s*(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|CONNECT|TRACE)\s+\S+\s+HTTP/\d\.\d"\s+(\d{3})\b',
    re.I,
)


def is_access_log_line(text: str) -> bool:
    return bool(_ACCESS_LOG.search(text or ""))


def access_log_status(text: str) -> int | None:
    m = _ACCESS_LOG.search(text or "")
    return int(m.group(1)) if m else None


def is_access_log_noise(text: str) -> bool:
    """2xx/3xx HTTP 访问日志（URL 里含 error 字样也算正常）。"""
    if not is_access_log_line(text):
        return False
    status = access_log_status(text)
    if status is None:
        return True
    return status < 400


def extract_log_level(line: str) -> str:
    """从原始日志行解析级别（支持 | INFO | 与 [INFO] 两种常见格式）。"""
    s = line or ""
    if is_access_log_line(s):
        status = access_log_status(s)
        if status is not None and status >= 500:
            return "ERROR"
        if status is not None and status >= 400:
            return "WARN"
        return "INFO"
    m = _PIPE_LEVEL.search(s)
    if m:
        return m.group(1).upper()
    m = _BRACKET_LEVEL.match(s.lstrip())
    if m:
        return m.group(1).upper()
    # 避免 URL 路径中的 error（如 /match-mq-send-error）被误判为级别
    if not is_access_log_line(s) and not re.search(r"https?://", s):
        m = re.search(r"\b(ERROR|WARN|FATAL)\b", s)
        if m:
            return m.group(1).upper()
    if re.search(r"\b(WARN|WARNING)\b", s, re.I):
        return "WARN"
    if re.search(r"\bINFO\b", s):
        return "INFO"
    if re.search(r"\bDEBUG\b", s):
        return "DEBUG"
    return ""


def is_debug_config_line(text: str) -> bool:
    if not text or len(text.strip()) < 4:
        return True
    if _DEBUG_CONFIG.search(text):
        return True
    # 纯配置 dump：大量 = 且无日志级别
    if text.count("=") >= 2 and not re.search(r"\b(ERROR|WARN|Exception)\b", text, re.I):
        if re.search(r"\{[a-z]+\}", text):
            return True
    if _CONFIG_NOISE.search(text):
        return True
    return False


def is_config_noise_line(text: str) -> bool:
    if is_access_log_noise(text):
        return True
    return is_debug_config_line(text) or is_sql_false_positive(text)


def is_sql_false_positive(text: str) -> bool:
    low = text.lower()
    if _SQL_ERROR.search(low):
        return True
    if "select " in low or "insert " in low or "update " in low or " from " in low:
        if "error" in low and not _REAL_ERROR.search(text):
            return True
    return False


def keyword_matches_line(keyword: str, line: str, app_level: str = "") -> bool:
    """判断日志行是否应计入关键字告警（减少 debug/SQL/配置误报）。"""
    from app.services.log_monitor.alert_digest import _clean_log_line

    kw = (keyword or "").strip().lower()
    if not kw:
        return False

    clean = _clean_log_line(line)
    if is_access_log_noise(line) or is_access_log_noise(clean):
        return False
    if is_config_noise_line(clean):
        return False

    level = (app_level or extract_log_level(line) or "").upper()
    if not level and is_debug_config_line(clean):
        level = "DEBUG"
    low = clean.lower()

    if kw == "error":
        if level in ("DEBUG", "TRACE", "INFO"):
            return False
        if level in ("ERROR", "FATAL"):
            return True
        if _REAL_ERROR.search(clean):
            if _EMBEDDED_ERROR.search(clean) and not re.search(r"\berror\b", low):
                return False
            return True
        if re.search(r"\berror\b", low):
            # URL/路径片段中的 error，非独立错误词
            if re.search(r"https?://[^\s\"']*error", low) and not _REAL_ERROR.search(clean):
                return False
            return True
        return False

    if kw == "exception":
        if _REAL_EXCEPTION.search(clean):
            return True
        if level in ("ERROR", "FATAL"):
            return "exception" in low and not is_sql_false_positive(clean)
        if level in ("WARN",):
            return bool(re.search(r"\w+Exception", clean))
        # INFO/DEBUG 中的 exception 多为配置/SQL，忽略
        return False

    if kw == "fail" or kw == "failed":
        if level in ("DEBUG", "INFO", "TRACE"):
            return False
        return bool(re.search(rf"\b{re.escape(kw)}", low))

    # 其他关键字：词边界匹配 + 非 DEBUG 配置
    if level in ("DEBUG", "TRACE") and level != "ERROR":
        return False
    return bool(re.search(rf"\b{re.escape(kw)}\b", low))


def immediate_keyword_matches_line(keyword: str, line: str) -> bool:
    """即时告警关键字：短语子串匹配（支持 Cause: xxx 等堆栈行）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    from app.services.log_monitor.alert_digest import _clean_log_line

    clean = _clean_log_line(line)
    if is_access_log_noise(line) or is_access_log_noise(clean):
        return False
    return kw in clean.lower()


def is_meaningful_alert_digest(digest: str) -> bool:
    """Slack 摘要是否包含可读的错误信息（非配置噪声）。"""
    if not digest or not digest.strip():
        return False
    body = re.sub(r"^\*[^*]+\*\n?", "", digest.strip())
    if not body.strip():
        return False
    if is_config_noise_line(body):
        return False
    return is_digest_worthy_line(body) or bool(
        re.search(r"\b(ERROR|FATAL|Exception|Caused by|failed|panic)\b", body, re.I)
    )


def parse_threshold_alert_meta(msg: str) -> tuple[int, int, str]:
    """从 THRESHOLD 告警 msg 解析 (count, window_sec, last_line)。"""
    text = msg or ""
    count, window = 0, 0
    m = re.search(r"\[THRESHOLD\s+(\d+)/(\d+)s\]", text)
    if m:
        count, window = int(m.group(1)), int(m.group(2))
    last = text.split("| Last:", 1)[1].strip() if "| Last:" in text else ""
    return count, window, last


def is_digest_worthy_line(clean: str) -> bool:
    """摘要中是否值得展示的行。"""
    if is_config_noise_line(clean):
        return False
    if _REAL_EXCEPTION.search(clean) or _REAL_ERROR.search(clean):
        return True
    if re.search(r"^\s*at ", clean):
        return True
    if re.search(r"Caused by\s*:", clean, re.I):
        return True
    return False
