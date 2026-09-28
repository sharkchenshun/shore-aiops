# ============================================================
# app/services/log_monitor/s3_helpers.py — S3 / 日志浏览辅助
# ============================================================

from __future__ import annotations

import codecs
import datetime
import json
import os
import re
from typing import Any
from uuid import UUID

from app.models.monitor import MonitorTask
from app.services.log_monitor.engine import monitor_engine


def get_s3_client(task: MonitorTask):
    try:
        import boto3
    except ImportError:
        return None
    if not task.s3_archive_enabled or not task.s3_bucket:
        return None
    try:
        endpoint = task.s3_endpoint
        if endpoint and ('s3.amazonaws.com' in endpoint or endpoint.strip() == ''):
            endpoint = None
        return boto3.client(
            's3',
            region_name=task.s3_region,
            aws_access_key_id=task.s3_access_key,
            aws_secret_access_key=task.s3_secret_key,
            endpoint_url=endpoint or None,
        )
    except Exception:
        return None


def task_s3_prefixes(task: MonitorTask) -> list[str]:
    """只允许读本任务目录。任务名不得作为 logs/monitor/ 的前缀，否则可跨任务读日志。"""
    prefixes = [f"logs/monitor/{task.id}/"]
    name = (task.name or "").strip()
    if name and re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name) and name.lower() != "monitor":
        prefixes.append(f"logs/{name}/")
    return prefixes


def iter_s3_lines(body_stream):
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    buf = ""
    for chunk in iter(lambda: body_stream.read(64 * 1024), b''):
        buf += decoder.decode(chunk)
        while True:
            nl = buf.find('\n')
            if nl == -1:
                break
            line = buf[:nl + 1]
            buf = buf[nl + 1:]
            yield line
    buf += decoder.decode(b'', final=True)
    if buf:
        yield buf


def latest_completed_4h_window_start(now: datetime.datetime) -> datetime.datetime:
    local_dt = _localtime(now)
    hour = (local_dt.hour // 4) * 4
    ws = local_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    return ws - datetime.timedelta(hours=4)


def index_s3_key(task: MonitorTask, log_type: str, window_start: datetime.datetime) -> str:
    ws = _localtime(window_start)
    date_str = ws.date().isoformat()
    start_h = ws.hour
    end_h = (start_h + 3) % 24
    return f"logs/monitor/{task.id}/indexes/{log_type}/{date_str}/{start_h:02d}00-{end_h:02d}59.json"


def _localtime(dt: datetime.datetime) -> datetime.datetime:
    from app.services.log_monitor.engine import _localtime as lt
    return lt(dt)


def handle_s3_error(e, task: MonitorTask, db_save) -> bool:
    try:
        import botocore
        if isinstance(e, botocore.exceptions.ClientError):
            err_code = e.response.get('Error', {}).get('Code')
            if err_code in ('301', 'PermanentRedirect'):
                correct_region = e.response.get('ResponseMetadata', {}).get('HTTPHeaders', {}).get('x-amz-bucket-region')
                if correct_region and correct_region != task.s3_region:
                    task.s3_region = correct_region
                    db_save(task)
                    return True
    except Exception:
        pass
    return False


def redact_task(task: MonitorTask) -> dict[str, Any]:
    d = {
        "id": str(task.id),
        "name": task.name,
        "enabled": task.enabled,
        "k8s_namespace": task.k8s_namespace,
        "environment_id": getattr(task, "environment_id", None) or "test",
        "k8s_kubeconfig": "",
        "k8s_kubeconfig_set": bool(task.k8s_kubeconfig),
        "s3_archive_enabled": task.s3_archive_enabled,
        "s3_bucket": task.s3_bucket,
        "s3_region": task.s3_region,
        "s3_access_key": f"****{task.s3_access_key[-4:]}" if task.s3_access_key and len(task.s3_access_key) >= 4 else ("****" if task.s3_access_key else None),
        "s3_secret_key": "",
        "s3_secret_key_set": bool(task.s3_secret_key),
        "s3_endpoint": task.s3_endpoint,
        "retention_days": task.retention_days,
        "alert_enabled": task.alert_enabled,
        "slack_webhook_url": "",
        "slack_webhook_set": bool(task.slack_webhook_url),
        "poll_interval_seconds": task.poll_interval_seconds,
        "alert_keywords": task.alert_keywords or [],
        "immediate_keywords": task.immediate_keywords or [],
        "ignore_keywords": task.ignore_keywords or [],
        "record_only_keywords": task.record_only_keywords or [],
        "alert_threshold_count": task.alert_threshold_count,
        "alert_threshold_window": task.alert_threshold_window,
        "alert_silence_minutes": task.alert_silence_minutes,
        "last_run": task.last_run.isoformat() if task.last_run else None,
        "last_error": task.last_error,
        "alerts_sent_count": task.alerts_sent_count,
        "alert_state": task.alert_state or {},
        "threshold_state": task.threshold_state or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }
    return d


def list_log_files(task: MonitorTask, *, page=1, page_size=10, search="", sort_by="mtime",
                   order="desc", log_type="all", realtime=False) -> dict:
    from app.services.log_monitor.engine import _now

    base_dir = monitor_engine.LOG_DIR
    s3_client = get_s3_client(task)
    s3_files: list[dict] = []
    local_files: list[dict] = []

    if s3_client:
        lt = log_type if log_type in ('raw', 'error') else 'raw'
        ws = latest_completed_4h_window_start(_now())
        payload = None
        if realtime:
            try:
                payload = monitor_engine.get_realtime_index_payload(task, lt)
            except Exception:
                pass
        if not payload:
            idx_key = index_s3_key(task, lt, ws)
            try:
                obj = s3_client.get_object(Bucket=task.s3_bucket, Key=idx_key)
                raw = obj['Body'].read().decode('utf-8', errors='replace')
                payload = json.loads(raw)
            except Exception as e:
                if handle_s3_error(e, task, lambda t: None):
                    s3_client = get_s3_client(task)
                    try:
                        obj = s3_client.get_object(Bucket=task.s3_bucket, Key=idx_key)
                        raw = obj['Body'].read().decode('utf-8', errors='replace')
                        payload = json.loads(raw)
                    except Exception:
                        pass
        if payload and isinstance(payload.get('files'), list):
            s3_files = payload['files']
            aggregated: dict[str, dict] = {}
            for f in s3_files:
                key = f.get('name') or ''
                if '/raw/' in key:
                    try:
                        idx = key.find('/raw/')
                        rest = key[idx + 5:]
                        p = rest.split('/')
                        if len(p) >= 4:
                            ns, pod = p[0], p[1]
                            virtual_name = f"{ns}_{pod}_s3_recent.log"
                            if virtual_name not in aggregated:
                                aggregated[virtual_name] = {
                                    "name": virtual_name, "size": 0, "mtime": 0,
                                    "is_virtual": True, "s3_keys": [],
                                }
                            aggregated[virtual_name]['size'] += int(f.get('size') or 0)
                            aggregated[virtual_name]['mtime'] = max(
                                aggregated[virtual_name]['mtime'], float(f.get('mtime') or 0)
                            )
                            aggregated[virtual_name]['s3_keys'].append(key)
                    except Exception:
                        pass
            if aggregated:
                s3_files = list(aggregated.values())

    log_dir = os.path.join(base_dir, str(task.id))
    if os.path.exists(log_dir):
        def is_error_name(name: str) -> bool:
            n = (name or '').lower()
            return n.endswith('_error.log') or ('task_errors_' in n)

        for f in os.listdir(log_dir):
            if not f.endswith('.log'):
                continue
            if search and search not in f.lower():
                continue
            if log_type == 'error' and not is_error_name(f):
                continue
            if log_type == 'raw' and is_error_name(f):
                continue
            full_path = os.path.join(log_dir, f)
            stat = os.stat(full_path)
            local_files.append({"name": f, "size": stat.st_size, "mtime": stat.st_mtime})

    files = s3_files + local_files
    if search:
        sq = search.lower()
        files = [f for f in files if sq in str((f.get('name') or '')).lower()]

    reverse = order == 'desc'
    if sort_by == 'name':
        files.sort(key=lambda x: x.get('name') or '', reverse=reverse)
    elif sort_by == 'size':
        files.sort(key=lambda x: int(x.get('size') or 0), reverse=reverse)
    else:
        files.sort(key=lambda x: float(x.get('mtime') or 0), reverse=reverse)

    total = len(files)
    start_i = (page - 1) * page_size
    paged = files[start_i:start_i + page_size]
    return {"files": paged, "total": total, "page": page, "page_size": page_size, "source": "hybrid", "realtime": realtime}
