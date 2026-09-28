import time
import os
import threading
import datetime
import glob
import logging
import json
import re
from datetime import timezone as dt_timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings as app_settings
from app.services.log_monitor import store as task_store
from app.services.log_monitor.log_keyword_match import (
    extract_log_level,
    immediate_keyword_matches_line,
    is_meaningful_alert_digest,
    keyword_matches_line,
    parse_threshold_alert_meta,
)
from app.services.log_monitor.alert_digest import (
    DEFAULT_TAIL_LINES,
    build_slack_digest_blocks,
    clean_log_line,
    extract_alert_line,
    summarize_error_log,
)

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger("monitor")

SLACK_ERROR_CONTEXT_MAX = 12000

# K8s timestamps=True: "2026-09-07T15:16:35.502402671+09:00 message..."
_LOG_LINE_TS_RE = re.compile(
    r"^(?P<head>\d{4}-\d{2}-\d{2}T[\d:]+)"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})?"
)


def _parse_log_line_timestamp(line: str) -> float | None:
    """Parse RFC3339 prefix from K8s pod log lines (handles nanoseconds + TZ)."""
    if not line:
        return None
    ts_part = line.split(" ", 1)[0]
    if "T" not in ts_part:
        return None
    try:
        m = _LOG_LINE_TS_RE.match(ts_part)
        if not m:
            return None
        frac = (m.group("frac") or "")[:6].ljust(6, "0")
        tz = m.group("tz") or "+00:00"
        if tz == "Z":
            tz = "+00:00"
        iso = f"{m.group('head')}.{frac}{tz}"
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _merge_log_context(*chunks: str) -> str:
    """合并本地采集日志与 K8s tail，去重且优先保留本地堆栈块。"""
    seen: set[str] = set()
    lines: list[str] = []
    for chunk in chunks:
        if not chunk or not chunk.strip():
            continue
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped or re.match(r"^-{10,}$", stripped):
                continue
            key = _clean_log_line(stripped) or stripped
            if key in seen:
                continue
            seen.add(key)
            lines.append(line.rstrip())
    return "\n".join(lines)


def _format_slack_error_context(error_lines: list[str] | None, *, max_chars: int = SLACK_ERROR_CONTEXT_MAX) -> str:
    if not error_lines:
        return ""
    text = "\n".join(line.rstrip("\n") for line in error_lines if line and line.strip())
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return f"...(前部省略 {len(text) - max_chars} 字符)\n" + text[-max_chars:]


def _read_file_tail(path: str, max_chars: int = SLACK_ERROR_CONTEXT_MAX) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max(max_chars * 2, 8192)))
            raw = f.read().decode("utf-8", errors="replace")
        if len(raw) <= max_chars:
            return raw.strip()
        return f"...(前部省略)\n" + raw[-max_chars:].strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("read_file_tail.failed path=%s error=%s", path, str(exc)[:80])
        return ""


def _now():
    return datetime.datetime.now(dt_timezone.utc)


def _pod_deployment_key(pod_name: str) -> str:
    """ReplicaSet 级别 key，同 Deployment 多副本共享静默。"""
    if not pod_name:
        return ""
    m = re.match(r"^(.+)-[a-z0-9]{5,10}$", pod_name)
    return m.group(1) if m else pod_name


def _alert_silence_db_key(keyword: str, namespace: str, pod_name: str) -> str:
    dep = _pod_deployment_key(pod_name)
    scope = f"{namespace}/{dep}" if namespace and dep else (dep or namespace or "global")
    return f"{keyword}::{scope}"


def _localtime(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def _make_aware(dt):
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=LOCAL_TZ)
try:
    from kubernetes import client, config as k8s_config
    from kubernetes.client.rest import ApiException
except ImportError:
    client = None
    k8s_config = None

try:
    import boto3
    from botocore.exceptions import NoCredentialsError
except ImportError:
    boto3 = None

class MonitorEngine:
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.LOG_DIR = app_settings.log_monitor_dir
        os.makedirs(self.LOG_DIR, exist_ok=True)
        self._index_lock = threading.Lock()
        self._index_entries = {}
        self._index_last_window_start = {}
        # S3 error alert throttle: task_id -> last_alert_timestamp
        self._s3_error_last_alert = {}
        self._s3_error_count = {}
        # 告警静默：task_id:keyword::ns/deployment -> last_sent_ts（内存 + DB 双写）
        self._alert_silence_cache: dict[str, float] = {}
        self._alert_silence_lock = threading.Lock()

    def _alert_silence_seconds(self, task) -> int:
        return max(0, (task.alert_silence_minutes or 60)) * 60

    def _alert_silence_last_sent(self, task, db_key: str) -> float:
        mem_key = f"{task.id}:{db_key}"
        with self._alert_silence_lock:
            last = self._alert_silence_cache.get(mem_key, 0.0)
        if last:
            return last
        alert_state = task.alert_state or {}
        return float(alert_state.get(db_key, 0) or 0)

    def _is_alert_silenced(self, task, keyword: str, namespace: str, pod_name: str, atype: str = "THRESHOLD") -> bool:
        if atype == "IMMEDIATE":
            return False
        silence_seconds = self._alert_silence_seconds(task)
        if silence_seconds <= 0:
            return False
        db_key = _alert_silence_db_key(keyword, namespace, pod_name)
        last_sent = self._alert_silence_last_sent(task, db_key)
        if not last_sent:
            return False
        return time.time() - last_sent <= silence_seconds

    def _mark_alert_sent(self, task, keyword: str, namespace: str, pod_name: str) -> None:
        db_key = _alert_silence_db_key(keyword, namespace, pod_name)
        mem_key = f"{task.id}:{db_key}"
        now = time.time()
        with self._alert_silence_lock:
            self._alert_silence_cache[mem_key] = now
            alert_state = dict(task.alert_state or {})
            alert_state[db_key] = now
            task.alert_state = alert_state
        task_store.save_task(task, ["alert_state"])

    def _get_s3_client(self, task):
        if not boto3:
            return None
        if not getattr(task, 's3_archive_enabled', False):
            return None
        if not getattr(task, 's3_bucket', ''):
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
                endpoint_url=endpoint or None
            )
        except Exception:
            return None

    def _get_s3_prefix(self, task):
        return f"logs/monitor/{task.id}/"

    def _get_4h_window_start(self, dt):
        local_dt = _localtime(dt)
        hour = (local_dt.hour // 4) * 4
        return local_dt.replace(hour=hour, minute=0, second=0, microsecond=0)

    def _get_latest_completed_window_start(self, now):
        return self._get_4h_window_start(now) - datetime.timedelta(hours=4)

    def _get_index_s3_key(self, task, log_type: str, window_start):
        ws = _localtime(window_start)
        date_str = ws.date().isoformat()
        start_h = ws.hour
        end_h = (start_h + 3) % 24
        return f"{self._get_s3_prefix(task)}indexes/{log_type}/{date_str}/{start_h:02d}00-{end_h:02d}59.json"

    def _record_index_entry(self, task, log_type: str, key: str, size_bytes: int, ts: float):
        try:
            if not key:
                return
            now = _now()
            ws = self._get_4h_window_start(now)
            tid = str(task.id)
            with self._index_lock:
                by_task = self._index_entries.setdefault(tid, {})
                by_window = by_task.setdefault(ws.isoformat(), {"raw": {}, "error": {}})
                by_type = by_window.setdefault(log_type, {})
                by_type[key] = {"name": key, "size": int(size_bytes or 0), "mtime": float(ts or now.timestamp())}
                self._index_last_window_start[tid] = ws.isoformat()
        except Exception:
            pass

    def _finalize_due_indexes(self, task):
        s3_client = self._get_s3_client(task)
        if not s3_client:
            return
        now = _now()
        latest_completed_ws = self._get_latest_completed_window_start(now)
        tid = str(task.id)

        to_finalize = []
        with self._index_lock:
            by_task = self._index_entries.get(tid) or {}
            for ws_iso in list(by_task.keys()):
                try:
                    ws = datetime.datetime.fromisoformat(ws_iso)
                except Exception:
                    continue
                if ws.tzinfo is None:
                    ws = _make_aware(ws)
                if ws <= latest_completed_ws:
                    to_finalize.append(ws)

        for ws in sorted(to_finalize):
            self._upload_index_file(task, s3_client, 'raw', ws)
            self._upload_index_file(task, s3_client, 'error', ws)
            with self._index_lock:
                by_task = self._index_entries.get(tid) or {}
                by_task.pop(ws.isoformat(), None)
                if not by_task:
                    self._index_entries.pop(tid, None)

    def _upload_index_file(self, task, s3_client, log_type: str, window_start):
        tid = str(task.id)
        ws = _localtime(window_start)
        we = ws + datetime.timedelta(hours=4) - datetime.timedelta(seconds=1)
        ws_iso = ws.isoformat()
        items = []
        with self._index_lock:
            by_task = self._index_entries.get(tid) or {}
            by_window = by_task.get(ws_iso) or {}
            items_map = by_window.get(log_type) or {}
            items = list(items_map.values())

        items.sort(key=lambda x: x.get('name') or '')
        payload = {
            "task_id": tid,
            "task_name": getattr(task, 'name', ''),
            "log_type": log_type,
            "window_start": ws_iso,
            "window_end": we.isoformat(),
            "generated_at": _now().isoformat(),
            "total": len(items),
            "files": items,
        }
        key = self._get_index_s3_key(task, log_type, ws)
        try:
            s3_client.put_object(
                Bucket=task.s3_bucket,
                Key=key,
                Body=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                ContentType='application/json; charset=utf-8'
            )
        except Exception:
            pass

    def get_realtime_index_payload(self, task, log_type: str):
        tid = str(getattr(task, 'id', '') or '')
        if not tid:
            return None
        now = _now()
        ws = self._get_4h_window_start(now)
        ws_iso = ws.isoformat()
        we = _localtime(ws) + datetime.timedelta(hours=4) - datetime.timedelta(seconds=1)

        with self._index_lock:
            by_task = self._index_entries.get(tid) or {}
            by_window = by_task.get(ws_iso)
            if not by_window:
                last_ws_iso = self._index_last_window_start.get(tid)
                if last_ws_iso and last_ws_iso in by_task:
                    ws_iso = last_ws_iso
                    try:
                        ws = datetime.datetime.fromisoformat(ws_iso)
                        if ws.tzinfo is None:
                            ws = _make_aware(ws)
                        we = _localtime(ws) + datetime.timedelta(hours=4) - datetime.timedelta(seconds=1)
                    except Exception:
                        ws = self._get_4h_window_start(now)
                        we = _localtime(ws) + datetime.timedelta(hours=4) - datetime.timedelta(seconds=1)
                    by_window = by_task.get(ws_iso)

            by_window = by_window or {}
            items_map = by_window.get(log_type) or {}
            items = list(items_map.values())

        items.sort(key=lambda x: x.get('name') or '')
        return {
            "task_id": tid,
            "task_name": getattr(task, 'name', ''),
            "log_type": log_type,
            "window_start": ws_iso,
            "window_end": we.isoformat(),
            "generated_at": _now().isoformat(),
            "total": len(items),
            "files": items,
            "realtime": True,
        }

    def _cleanup_s3(self, task, s3_client, retention_days=90, max_delete=1000):
        try:
            cutoff = _now() - datetime.timedelta(days=max(1, int(retention_days or 1)))
            prefix = self._get_s3_prefix(task)
            keys_to_delete = []
            paginator = s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=task.s3_bucket, Prefix=prefix):
                for obj in page.get('Contents', []) or []:
                    lm = obj.get('LastModified')
                    if lm and lm < cutoff:
                        keys_to_delete.append({'Key': obj['Key']})
                        if len(keys_to_delete) >= max_delete:
                            break
                if len(keys_to_delete) >= max_delete:
                    break

            if keys_to_delete:
                for i in range(0, len(keys_to_delete), 1000):
                    s3_client.delete_objects(
                        Bucket=task.s3_bucket,
                        Delete={'Objects': keys_to_delete[i:i + 1000], 'Quiet': True}
                    )
        except Exception as e:
            logger.error(f"Failed to cleanup S3 for task {task.id}: {e}")
            self._alert_s3_failure(task, str(e))

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            logger.info("Monitor engine started (v2-patched)")

    def stop(self):
        self._stop_event.set()
        with self._lock:
            if self._thread:
                self._thread.join(timeout=5)
                self._thread = None
        logger.info("Monitor engine stopped")

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                # Iterate over all enabled tasks
                tasks = task_store.get_enabled_tasks()
                if not tasks:
                    time.sleep(5)
                    continue

                for task in tasks:
                    if self._stop_event.is_set():
                        break
                        
                    # Basic scheduling: run if last_run is older than interval
                    # Or just run every loop and let loop sleep handle it?
                    # Better: Check timestamp.
                    now = _now()
                    
                    # Convert last_run to offset-naive if needed, or handle timezone
                    # Simplified: if last_run is None or (now - last_run).total_seconds() > poll_interval
                    should_run = False
                    if not task.last_run:
                        should_run = True
                    else:
                        # Assuming naive or matching tz
                        try:
                            delta = now.timestamp() - task.last_run.timestamp()
                            interval = max(10, int(task.poll_interval_seconds or 60))
                            if delta >= interval:
                                should_run = True
                        except:
                            should_run = True # Fallback
                            
                    if should_run:
                        try:
                            self._process_task(task)
                            task.last_run = now
                            task.last_error = "" # clear error on success
                            task_store.save_task(task, ['last_run', 'last_error', 'alerts_sent_count'])
                        except Exception as e:
                            logger.error(f"Error processing task {task.name}: {e}")
                            task.last_error = str(e)[:500]
                            task_store.save_task(task, ['last_error'])

                    try:
                        self._finalize_due_indexes(task)
                    except Exception:
                        pass
                
                # Global sleep - maybe shorter to check for new tasks/schedule
                time.sleep(5)
                
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                time.sleep(10)

    def _process_task(self, task):
        try:
            task = task_store.refresh_task(task, [
                'enabled',
                'k8s_namespace',
                'k8s_kubeconfig',
                'alert_enabled',
                'slack_webhook_url',
                'poll_interval_seconds',
                'alert_keywords',
                'immediate_keywords',
                'ignore_keywords',
                'record_only_keywords',
                'alert_threshold_count',
                'alert_threshold_window',
                'alert_silence_minutes',
            ])
        except Exception:
            pass
        # 1. Fetch Logs
        self._fetch_k8s_logs(task)
        
        # 2. Rotate & Archive
        self._rotate_and_archive(task)

    def _kubeconfig_content_for_task(self, task) -> str:
        env_id = getattr(task, "environment_id", None) or "test"
        from app.services.environments import get_profile
        from app.services.kubeconfig_store import require_kubeconfig_content

        profile = get_profile(env_id)
        if not profile:
            raise RuntimeError(f"未知环境: {env_id}")
        return require_kubeconfig_content(profile)

    def _get_k8s_client(self, task):
        if not client:
            raise ImportError("kubernetes package not installed")

        import yaml

        if task.k8s_kubeconfig:
            k8s_config.load_kube_config_from_dict(yaml.safe_load(task.k8s_kubeconfig))
        else:
            try:
                k8s_config.load_incluster_config()
            except Exception:
                content = self._kubeconfig_content_for_task(task)
                k8s_config.load_kube_config_from_dict(yaml.safe_load(content))

        return client.CoreV1Api()

    def _fetch_pod_log_tail(self, task, namespace, pod_name, container_name, tail_lines=DEFAULT_TAIL_LINES):
        """告警时重新拉取 Pod 日志尾部，获取完整堆栈。"""
        if not namespace or not pod_name or not container_name:
            return ""
        try:
            api = self._get_k8s_client(task)
            raw = api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container_name,
                tail_lines=tail_lines,
                timestamps=False,
            )
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            return raw or ""
        except Exception as exc:
            logger.debug(
                "fetch_pod_log_tail.failed pod=%s ns=%s error=%s",
                pod_name,
                namespace,
                str(exc)[:120],
            )
            return ""

    def _fetch_k8s_logs(self, task):
        api = self._get_k8s_client(task)
        # Support multiple namespaces comma-separated
        raw_namespaces = task.k8s_namespace.split(',')
        namespaces = [n.strip() for n in raw_namespaces if n.strip()]
        if not namespaces:
            namespaces = ['default']
        
        today_str = _localtime(_now()).date().isoformat()
        
        # User requirement: "Don't store locally, only error logs store locally."
        # This implies Raw Logs should go to S3 if enabled, or be discarded/kept in memory?
        # Assuming if S3 enabled -> S3. If S3 disabled -> Local (fallback).
        
        s3_client = self._get_s3_client(task)
        s3_only = bool(s3_client) 

        task_log_dir = os.path.join(self.LOG_DIR, str(task.id))
        os.makedirs(task_log_dir, exist_ok=True)

        ns_errors: list[str] = []
        pods_processed = 0

        for namespace in namespaces:
            try:
                pods = api.list_namespaced_pod(namespace)
            except Exception as e:
                err = f"{namespace}: {e}"
                logger.error(f"Failed to list pods in namespace {namespace}: {e}")
                ns_errors.append(err)
                continue

            for pod in pods.items:
                pod_name = pod.metadata.name
                if not pod.spec.containers:
                    continue
                container_name = pod.spec.containers[0].name
                
                unique_name = f"{namespace}_{pod_name}"
                
                # Raw Log Path
                # If S3 is enabled, raw logs go to S3 (no local).
                # If S3 is disabled, raw logs go to Local.
                log_file_path = ""
                if not s3_only:
                     # Use daily rotation for local raw logs if S3 disabled
                     log_file_path = os.path.join(task_log_dir, f"{unique_name}_{today_str}.log")
                
                since_seconds = task.poll_interval_seconds + 10
                
                try:
                    # Use streaming to avoid loading huge logs into memory
                    response = api.read_namespaced_pod_log(
                        name=pod_name,
                        namespace=namespace,
                        container=container_name,
                        since_seconds=since_seconds,
                        timestamps=True,
                        _preload_content=False # Enable streaming
                    )
                    
                    if s3_only:
                        # Prepare S3 Keys for Raw Logs
                        stamp = _now().strftime("%H%M%S_%f")
                        base_prefix = self._get_s3_prefix(task).rstrip('/')
                        # Use raw/namespace/pod/date/stamp.log structure
                        raw_s3_key = f"{base_prefix}/raw/{namespace}/{pod_name}/{today_str}/{stamp}.log"
                        # Error logs are always local, but we can also backup to S3 if desired.
                        # For now, let's keep error logs LOCAL as primary, and optional S3 backup in _process_log_stream
                        
                        self._process_log_stream(
                            response,
                            task,
                            unique_name,
                            task_log_dir,
                            log_file_path, # Empty string means don't write raw to local
                            s3_client=s3_client,
                            s3_bucket=task.s3_bucket,
                            raw_s3_key=raw_s3_key,
                            namespace=namespace,
                            pod_name=pod_name,
                            container_name=container_name,
                        )
                    else:
                        # Local Mode
                        self._process_log_stream(
                            response, task, unique_name, task_log_dir, log_file_path,
                            namespace=namespace,
                            pod_name=pod_name,
                            container_name=container_name,
                        )
                        
                except Exception as e:
                    # logger.warning(f"Failed to read log for {pod_name}: {e}")
                    pass
                else:
                    pods_processed += 1

        if ns_errors and pods_processed == 0:
            msg = "; ".join(ns_errors)
            if "401" in msg or "Unauthorized" in msg:
                raise RuntimeError(
                    f"K8s 凭证失效 ({msg})，请在 设置 → K8s 集群凭证 更新 {getattr(task, 'environment_id', 'dev')} 环境 kubeconfig"
                )
            raise RuntimeError(f"无法拉取 Pod 日志: {msg}")

    def _process_log_stream(
        self, stream, task, source_name, log_dir, log_file_path,
        s3_client=None, s3_bucket=None, raw_s3_key=None, error_s3_key=None,
        namespace="", pod_name="", container_name="",
    ):
        alerts = [] # List of dicts: { 'type': str, 'keyword': str, 'msg': str }
        error_lines = [] # List of strings for aggregated error log
        if source_name and "_" in source_name:
            namespace = namespace or source_name.split("_", 1)[0]
            pod_name = pod_name or source_name.split("_", 1)[1]
        elif source_name:
            namespace = namespace or source_name
        
        # Determine modes
        # 1. Raw Logs: S3 or Local?
        write_raw_s3 = bool(s3_client and s3_bucket and raw_s3_key)
        write_raw_local = bool(log_file_path)
        
        # 2. Error Logs: Always Local (as per requirement), Optional S3 backup
        # error_s3_key is optional backup
        
        raw_buffer = []
        error_output = []
        stream_line_count = 0
        raw_byte_count = 0
        max_lines = app_settings.LOG_MONITOR_MAX_LINES_PER_POLL
        max_raw_bytes = app_settings.LOG_MONITOR_MAX_RAW_BYTES
        max_alerts = app_settings.LOG_MONITOR_MAX_ALERTS_PER_BATCH
        stream_truncated = False
        severity = os.environ.get("SHARK_AIOPS_LOG_SEVERITY", "all").strip().lower()
        SEVERITY_ORDER = {'DEBUG': 0, 'TRACE': 0, 'INFO': 10, 'WARN': 20, 'ERROR': 30, 'FATAL': 40}
        _severity_min_level = 0
        _severity_keywords = []
        if severity == "error":
            _severity_min_level = 30
            _severity_keywords = ['error', 'fatal', 'panic']
        elif severity == "warn":
            _severity_min_level = 20
            _severity_keywords = ['error', 'warn', 'fatal', 'panic']
        
        # Load persistent threshold state from task if available, or init new
        threshold_state = task.threshold_state or {}
        threshold_window = task.alert_threshold_window # e.g. 60
        threshold_count = task.alert_threshold_count or 1
        threshold_count = max(1, min(threshold_count, 20))
        threshold_updated = False
        
        # Keywords
        # Model fields are JSONField (list of strings), e.g. ["error", "exception"]
        # We need to handle them as lists, not splitlines() on text.
        
        def parse_keywords(field_value):
            if not field_value:
                return []
            if isinstance(field_value, list):
                return [str(k).strip() for k in field_value if str(k).strip()]
            if isinstance(field_value, str):
                return [k.strip() for k in field_value.splitlines() if k.strip()]
            return []

        clean_immediate_keywords = [k.lower() for k in parse_keywords(task.immediate_keywords)]
        clean_alert_keywords = [k.lower() for k in parse_keywords(task.alert_keywords)]
        clean_record_keywords = parse_keywords(task.record_only_keywords)
        clean_ignore_keywords = parse_keywords(task.ignore_keywords)
        
        # Add 'fata' to default errors (common in Go apps)
        default_error_keywords = ['error', 'exception', 'fail', 'fatal', 'panic', 'fata']
        
        # Stats
        count_error = 0
        count_warn = 0
        count_info = 0
        count_other = 0
        
        CONTEXT_LINES = 30
        context_buffer = [] # list of strings
        
        # When an error occurs, we need to record:
        # 1. The context (previous N lines)
        # 2. The error line
        # 3. The following N lines (future)
        # To handle multiple errors close to each other, we use a counter "lines_to_record_counter"
        
        after_context_counter = 0
        stack_capture_active = False

        def _strip_ansi(s: str) -> str:
            try:
                return re.sub(r'\x1b\[[0-9;]*m', '', s)
            except Exception:
                return s

        def _strip_k8s_timestamp(s: str) -> str:
            try:
                payload = s.lstrip()
                m = re.match(r'^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})\s+(.*)$', payload)
                if m:
                    return m.group(1)
            except Exception:
                pass
            return s

        def _extract_level(s: str):
            try:
                level = extract_log_level(s)
                return level or None
            except Exception:
                return None

        def _drop_bracket_tags(s: str) -> str:
            try:
                return re.sub(r'^(\[[^\]]+\]\s+)+', '', s.lstrip())
            except Exception:
                return s

        def _strip_leading_timestamp(s: str) -> str:
            try:
                payload = _strip_k8s_timestamp(_strip_ansi(s)).lstrip()
                for prefix in ('[ERROR]', '[WARN]', '[INFO]', '[DEBUG]', '[TRACE]', '[FATAL]'):
                    if payload.startswith(prefix):
                        payload = payload[len(prefix):].lstrip()
                        break

                if len(payload) >= 21 and payload[4] == '-' and payload[7] == '-' and ('T' in payload[:12]):
                    parts = payload.split(None, 1)
                    if len(parts) == 2 and parts[0].count(':') >= 2:
                        return parts[1]
            except Exception:
                pass
            return payload if 'payload' in locals() else s

        def _strip_level_prefix(s: str) -> str:
            try:
                payload = _strip_k8s_timestamp(_strip_ansi(s)).lstrip()
                for prefix in ('[ERROR]', '[WARN]', '[INFO]', '[DEBUG]', '[TRACE]', '[FATAL]'):
                    if payload.startswith(prefix):
                        return payload[len(prefix):].lstrip()
            except Exception:
                pass
            return s

        def _looks_like_java_frame(s: str) -> bool:
            t = s.strip()
            if not t:
                return False
            return bool(re.match(r'^[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)+\([A-Za-z0-9_$.]+:\d+\)$', t))

        def _format_stack_line(raw_line: str) -> str:
            payload = _strip_leading_timestamp(raw_line).rstrip('\n')
            payload = _drop_bracket_tags(payload).rstrip()
            if not payload:
                return "\n"
            if payload.startswith('at '):
                return f"    {payload}\n"
            if payload.startswith('Caused by') or payload.startswith('Suppressed:'):
                return f"{payload}\n"
            if payload.startswith('...') and payload.endswith('more'):
                return f"    {payload}\n"
            if _looks_like_java_frame(payload):
                return f"    at {payload}\n"
            return f"    {payload}\n"

        def _format_main_line(raw_line: str) -> str:
            payload = _strip_leading_timestamp(raw_line).rstrip('\n')
            payload = payload.rstrip()
            return f"{payload}\n" if payload else "\n"

        def _format_ctx_line(raw_line: str) -> str:
            payload = _strip_leading_timestamp(raw_line).rstrip('\n').rstrip()
            return payload

        def _is_stack_line(s: str) -> bool:
            payload = _strip_leading_timestamp(s).rstrip('\n')
            t = _drop_bracket_tags(payload).lstrip()
            if not t:
                return True
            if t.startswith('at '):
                return True
            if t.startswith('Caused by') or t.startswith('Suppressed:'):
                return True
            if t.startswith('...') and t.endswith('more'):
                return True
            if t.startswith('Traceback (most recent call last):'):
                return True
            if t.startswith('During handling of the above exception') or t.startswith('The above exception was the direct cause'):
                return True
            if t.startswith('goroutine ') or t.startswith('panic:'):
                return True
            if t.startswith('File "') or t.startswith('File '):
                return True
            if t.startswith('###') or t.startswith(';'):
                return True
            if 'nested exception is ' in t:
                return True
            if _looks_like_java_frame(t):
                return True
            return False
        
        # Separate file for errors: pod_name_YYYY-MM-DD_error.log
        today_str = _localtime(_now()).date().isoformat()
        error_file_path = os.path.join(log_dir, f"{source_name}_{today_str}_error.log")
        
        error_file_handle = None
        log_file_handle = None
        try:
            # ALWAYS Open Error Log File (Local)
            error_file_handle = open(error_file_path, "a", encoding="utf-8")
            
            # Open Raw Log File (Local) Only if configured
            if write_raw_local:
                 log_file_handle = open(log_file_path, "a", encoding="utf-8")
            
            for line_bytes in stream:
                if not line_bytes:
                    continue
                stream_line_count += 1
                if stream_line_count > max_lines:
                    stream_truncated = True
                    break
                    
                # Decode bytes to string
                line_raw = line_bytes.decode('utf-8', errors='replace')
                line = _strip_ansi(line_raw)
                
                # Severity filter: skip low-severity lines when not capturing
                if _severity_min_level > 0 and not stack_capture_active:
                    app_level_pre = _extract_level(line)
                    if app_level_pre:
                        if SEVERITY_ORDER.get(app_level_pre, 0) < _severity_min_level:
                            low = line.lower()
                            if not any(kw in low for kw in _severity_keywords):
                                continue
                    elif _severity_keywords:
                        low = line.lower()
                        if not any(kw in low for kw in _severity_keywords):
                            continue
                
                # Write to raw log
                if write_raw_s3:
                    line_bytes_len = len(line.encode("utf-8", errors="replace"))
                    if raw_byte_count + line_bytes_len <= max_raw_bytes:
                        raw_buffer.append(line)
                        raw_byte_count += line_bytes_len
                    elif not stream_truncated:
                        stream_truncated = True
                
                if write_raw_local and log_file_handle:
                    log_file_handle.write(line)

                # --- Analysis Logic Per Line ---
                if any(k in line for k in clean_ignore_keywords):
                    continue

                app_level = _extract_level(line)
                payload_lower = _strip_leading_timestamp(line).lower()
                line_lower = payload_lower
                is_stack_line = _is_stack_line(line)

                if stack_capture_active and is_stack_line:
                    if clean_immediate_keywords:
                        for k in clean_immediate_keywords:
                            if immediate_keyword_matches_line(k, line):
                                alerts.append({
                                    'type': 'IMMEDIATE',
                                    'keyword': k,
                                    'msg': f"[IMMEDIATE] {line}",
                                })
                                break

                    formatted = _format_stack_line(line)
                    error_lines.append(formatted.rstrip('\n'))
                    if write_raw_s3:
                        error_output.append(formatted)
                    
                    # Always write error to local file
                    error_file_handle.write(formatted)

                    context_buffer.append(line)
                    if len(context_buffer) > CONTEXT_LINES:
                        context_buffer.pop(0)

                    error_file_handle.flush()
                    continue
                
                # Simple counting
                if app_level in ('ERROR', 'FATAL') or any(
                    keyword_matches_line(k, line, app_level) for k in ('error', 'fail', 'exception')
                ):
                    count_error += 1
                elif 'warn' in line_lower:
                    count_warn += 1
                elif 'info' in line_lower:
                    count_info += 1
                else:
                    count_other += 1

                # Check for "Trigger" conditions (Alerts, Errors, Record Only)
                
                # 1. Immediate Alerts
                is_alert = False
                if clean_immediate_keywords:
                    for k in clean_immediate_keywords:
                        if immediate_keyword_matches_line(k, line):
                            alerts.append({
                                'type': 'IMMEDIATE',
                                'keyword': k,
                                'msg': f"[IMMEDIATE] {line}"
                            })
                            is_alert = True
                            break # Match first keyword only per line to avoid dupes
                
                # 2. Threshold Alerts
                current_ts = _parse_log_line_timestamp(line) or time.time()

                if clean_alert_keywords:
                    for k in clean_alert_keywords:
                        matched = keyword_matches_line(k, line, app_level)
                        if not matched and k == "error" and app_level in ("ERROR", "FATAL"):
                            matched = True
                        if not matched and k == "exception" and app_level in ("ERROR", "FATAL") and "exception" in line_lower:
                            matched = True
                        if matched:
                            if k not in threshold_state:
                                threshold_state[k] = []
                            threshold_state[k].append(current_ts)
                            # Cleanup old timestamps
                            threshold_state[k] = [t for t in threshold_state[k] if current_ts - t <= threshold_window]
                            threshold_updated = True
                            
                            if len(threshold_state[k]) >= threshold_count:
                                if not self._is_alert_silenced(task, k, namespace, pod_name, "THRESHOLD"):
                                    alerts.append({
                                        'type': 'THRESHOLD',
                                        'keyword': k,
                                        'msg': f"[THRESHOLD {len(threshold_state[k])}/{threshold_window}s] Keyword: '{k}' | Last: {line}"
                                    })
                                    is_alert = True
                                threshold_state[k] = [] # Reset after burst (sent or silenced)
                
                # 3. Generic Errors (for context recording)
                is_error = app_level in ('ERROR', 'FATAL') or any(
                    keyword_matches_line(k, line, app_level) for k in default_error_keywords
                )
                
                # 4. Record Only (and Suppress Alert)
                is_record = False
                if clean_record_keywords:
                     if any(k in line for k in clean_record_keywords):
                         is_record = True
                         
                         # CRITICAL FIX: If matched Record Only, remove any alerts generated by this line
                         # This allows "muting" specific errors that match general Alert keywords (like 'error')
                         # but shouldn't trigger Slack notifications.
                         if is_alert:
                             # Remove alerts added in step 1 & 2 for this line
                             # We can check the last added alerts or filter the whole list?
                             # Since we are processing line by line, the alerts for THIS line are added just now.
                             # But 'alerts' is a list for the whole stream/batch.
                             # We need to know which alerts correspond to THIS line.
                             # The alert dict has 'msg' which contains the line.
                             # Let's filter out alerts that contain this line text AND were just added?
                             # Simpler: Filter alerts list at the end of loop iteration? 
                             # No, alerts is accumulated for the whole pod stream.
                             
                             # Let's remove alerts where msg contains this line content.
                             # Be careful not to remove duplicates if same line appeared before?
                             # But here we are inside the loop for THIS line.
                             
                             # Actually, simpler way:
                             # Don't add to alerts if it matches record keywords?
                             # But we already added them in Step 1 & 2.
                             
                             # So let's remove them now.
                             alerts = [a for a in alerts if a['msg'].find(line) == -1]
                             is_alert = False # Reset flag so it doesn't prefix [ALERT] in log file

                if stack_capture_active and not is_stack_line:
                    stack_capture_active = False
                    after_context_counter = CONTEXT_LINES

                should_trigger_context = is_alert or is_error or is_record
                capture_active = stack_capture_active or after_context_counter > 0

                if should_trigger_context:
                    if not capture_active:
                        if write_raw_s3:
                            error_output.append("-" * 40 + "\n")
                        
                        # Always local
                        error_file_handle.write("-" * 40 + "\n")
                        
                        error_lines.append("-" * 40)
                        for ctx_line in context_buffer:
                            ctx_payload = _format_ctx_line(ctx_line)
                            if write_raw_s3:
                                error_output.append(f"{ctx_payload}\n")
                            
                            # Always local
                            error_file_handle.write(f"{ctx_payload}\n")
                            
                            error_lines.append(ctx_payload)

                    main_formatted = _format_main_line(line)
                    if write_raw_s3:
                        error_output.append(main_formatted)
                    
                    # Always local
                    error_file_handle.write(main_formatted)
                    
                    error_lines.append(main_formatted.rstrip('\n'))

                    stack_capture_active = bool(is_error)
                    after_context_counter = 0

                else:
                    if not stack_capture_active and after_context_counter > 0:
                        ctx_payload = _format_ctx_line(line)
                        if write_raw_s3:
                            error_output.append(f"{ctx_payload}\n")
                        
                        # Always local
                        error_file_handle.write(f"{ctx_payload}\n")
                        
                        error_lines.append(ctx_payload)
                        after_context_counter -= 1
                
                # Update context buffer (always keep last N lines)
                context_buffer.append(line)
                if len(context_buffer) > CONTEXT_LINES:
                    context_buffer.pop(0)
                    
                # Flush error file periodically or on write? 
                # Python's file object is buffered, let's flush on trigger to be safe
                if should_trigger_context or stack_capture_active:
                    error_file_handle.flush()

        finally:
            if error_file_handle:
                error_file_handle.close()
            if log_file_handle:
                log_file_handle.close()
                
            # Save threshold state if updated
            if threshold_updated:
                task.threshold_state = threshold_state
                task_store.save_task(task, ['threshold_state'])

        uploaded_error_key = None
        if write_raw_s3:
            try:
                if raw_buffer:
                    raw_body = "".join(raw_buffer).encode('utf-8')
                    s3_client.put_object(
                        Bucket=s3_bucket,
                        Key=raw_s3_key,
                        Body=raw_body,
                        ContentType='text/plain; charset=utf-8'
                    )
                    self._record_index_entry(task, 'raw', raw_s3_key, len(raw_body), time.time())
            except Exception:
                pass

            if error_s3_key and error_output:
                try:
                    err_body = "".join(error_output).encode('utf-8')
                    s3_client.put_object(
                        Bucket=s3_bucket,
                        Key=error_s3_key,
                        Body=err_body,
                        ContentType='text/plain; charset=utf-8'
                    )
                    uploaded_error_key = error_s3_key
                    self._record_index_entry(task, 'error', error_s3_key, len(err_body), time.time())
                except Exception:
                    pass

        if alerts:
            if len(alerts) > max_alerts:
                dropped = len(alerts) - max_alerts
                alerts = alerts[:max_alerts]
                alerts.append({
                    "type": "SYSTEM",
                    "keyword": "rate_limit",
                    "msg": f"[SYSTEM] 告警过多已截断，丢弃 {dropped} 条",
                })
            error_ref = uploaded_error_key or os.path.basename(error_file_path)
            self._send_slack_alert(
                alerts, task, source_name, log_dir, error_ref,
                error_lines=error_lines,
                error_file_path=error_file_path,
                namespace=namespace,
                pod_name=pod_name,
                container_name=container_name,
            )
            logger.info(
                "monitor.alerts.triggered task=%s pod=%s count=%d types=%s",
                task.name,
                source_name,
                len(alerts),
                ",".join({a.get("type", "?") for a in alerts}),
            )

        self._write_scan_postprocess(
            source_name, log_dir, count_error, count_warn, count_info, count_other, alerts, error_lines,
            truncated=stream_truncated,
        )

    def _write_scan_postprocess(self, source_name, log_dir, count_error, count_warn, count_info, count_other, alerts, error_lines, truncated=False):
        scan_history_path = os.path.join(log_dir, "scan_history.log")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trunc_note = " truncated=1" if truncated else ""
        log_entry = (
            f"[{timestamp}] [monitor] Pod: {source_name} | "
            f"counts error={count_error} warn={count_warn} info={count_info} other={count_other} alerts={len(alerts)}{trunc_note}\n"
        )
        try:
            with open(scan_history_path, "a", encoding="utf-8") as f:
                f.write(log_entry)
        except Exception as e:
            logger.error(f"Failed to write scan history: {e}")

        if error_lines:
            today_str = _localtime(_now()).date().isoformat()
            task_err_path = os.path.join(log_dir, f"task_errors_{today_str}.log")
            try:
                with open(task_err_path, "a", encoding="utf-8") as f:
                    f.write(f"===== {source_name} =====\n")
                    for line in error_lines:
                        f.write(line if line.endswith('\n') else f"{line}\n")
            except Exception as e:
                logger.error(f"Failed to write aggregated error log: {e}")

    def _analyze_logs(self, log_content, task, source_name, log_dir):
        # DEPRECATED: Kept only if needed for non-streaming fallback, 
        # but _fetch_k8s_logs now uses _process_log_stream.
        pass

    def _alert_s3_failure(self, task, error_msg, failed_count=1):
        """Send Slack alert on S3 upload failures. Throttled: max 1 alert per 30 min per task."""
        import httpx as req
        try:
            task = task_store.refresh_task(task, ['alert_enabled', 'slack_webhook_url'])
        except Exception:
            pass
        if not task.alert_enabled:
            return
        webhook_url = (task.slack_webhook_url or "").strip()
        if not webhook_url.startswith(("http://", "https://")):
            return
        
        now = time.time()
        tid = str(task.id)
        last = self._s3_error_last_alert.get(tid, 0)
        if now - last < 1800:  # 30 min cooldown
            return
        
        self._s3_error_last_alert[tid] = now
        bucket = task.s3_bucket or "?"
        region = task.s3_region or "?"
        
        text = (
            f":warning: *S3 Upload Failure*\n"
            f"*Task*: {task.name} (id={task.id})\n"
            f"*Bucket*: {bucket}  *Region*: {region}\n"
            f"*Error*: {str(error_msg)[:500]}\n"
            f"*Files failed*: {failed_count} in this batch\n"
            f"Check S3 credentials: `s3_access_key` in `monitor_monitortask` table."
        )
        try:
            req.post(webhook_url, json={"text": text}, timeout=5, follow_redirects=False)
        except Exception:
            pass

    def _send_slack_alert(self, alerts, task, source, log_dir, error_filename=None,
                          error_lines=None, error_file_path=None,
                          namespace="", pod_name="", container_name=""):
        try:
            task = task_store.refresh_task(task, ['alert_enabled', 'slack_webhook_url', 'alert_silence_minutes', 'alert_state'])
        except Exception:
            pass
        if not task.alert_enabled:
            return

        webhook_url = (task.slack_webhook_url or app_settings.SLACK_WEBHOOK_URL or "").strip()
        if not webhook_url.startswith(("http://", "https://")):
            return
            
        # --- Deduplication (Alert Silence) ---
        # THRESHOLD: 同 keyword + namespace + deployment 在静默期内只发一次
        # IMMEDIATE: 始终发送
        
        filtered_alerts = []
        
        for alert in alerts:
            atype = alert.get('type')
            keyword = alert.get('keyword')
            
            if atype == 'IMMEDIATE':
                filtered_alerts.append(alert)
            elif not self._is_alert_silenced(task, keyword, namespace, pod_name, atype):
                filtered_alerts.append(alert)
            else:
                db_key = _alert_silence_db_key(keyword, namespace, pod_name)
                last_sent = self._alert_silence_last_sent(task, db_key)
                logger.info(
                    "Silenced alert for '%s' on %s/%s (last sent: %s)",
                    keyword,
                    namespace,
                    _pod_deployment_key(pod_name) or pod_name,
                    datetime.datetime.fromtimestamp(last_sent) if last_sent else "unknown",
                )

        if not filtered_alerts:
            return

        raw_tail = self._fetch_pod_log_tail(
            task, namespace, pod_name, container_name, DEFAULT_TAIL_LINES,
        )
        fallback = _format_slack_error_context(error_lines, max_chars=SLACK_ERROR_CONTEXT_MAX)
        if not fallback and error_file_path:
            fallback = _read_file_tail(error_file_path, max_chars=SLACK_ERROR_CONTEXT_MAX)
        if not fallback and error_filename:
            local_path = os.path.join(log_dir, os.path.basename(str(error_filename)))
            fallback = _read_file_tail(local_path, max_chars=SLACK_ERROR_CONTEXT_MAX)

        primary = filtered_alerts[0]
        hint_line = extract_alert_line(primary.get("msg") or "")

        # 本地 error 文件含堆栈采集；K8s tail 作补充，合并后摘要
        combined = _merge_log_context(fallback, raw_tail)
        digest = summarize_error_log(combined, hint_line=hint_line) if combined.strip() else ""

        atype = primary.get("type", "ALERT")
        keyword = primary.get("keyword", "")
        primary_msg = primary.get("msg") or ""
        if not digest or not is_meaningful_alert_digest(digest):
            if atype == "THRESHOLD":
                cnt, win, last = parse_threshold_alert_meta(primary_msg)
                last_clean = clean_log_line(extract_alert_line(primary_msg))
                digest = (
                    f"*阈值触发*\n{keyword} 在 {win}s 内出现 {cnt} 次\n\n"
                    f"*说明*\n未找到有效 ERROR/Exception 日志，疑似配置/调试输出误触关键字。\n"
                    f"最近匹配行：\n`{last_clean[:300]}`"
                    if last_clean else
                    f"*阈值触发*\n{keyword} 在 {win}s 内出现 {cnt} 次\n\n"
                    f"*说明*\n未找到有效错误日志，请查看完整日志确认。"
                )
            else:
                trigger_msg = clean_log_line(extract_alert_line(primary_msg))
                digest = f"*根因*\n{trigger_msg[:500]}" if trigger_msg else ""

        log_url = None
        if error_filename:
            base_url = os.environ.get("PUBLIC_URL") or app_settings.PUBLIC_URL or "http://localhost:3456"
            log_url = f"{base_url.rstrip('/')}/logs?taskId={task.id}&filename={quote(str(error_filename), safe='')}"

        blocks = build_slack_digest_blocks(
            source=source,
            task_name=task.name,
            namespace=namespace or (source.split("_", 1)[0] if source else ""),
            pod_name=pod_name or source,
            alert_type=atype,
            keyword=keyword,
            digest=digest,
            log_url=log_url,
        )

        if len(filtered_alerts) > 1:
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"… 另有 {len(filtered_alerts) - 1} 条告警已合并",
                }],
            })

        try:
            payload = {"blocks": blocks}
            resp = httpx.post(webhook_url, json=payload, timeout=10, follow_redirects=False)
            if resp.status_code >= 400:
                logger.error(
                    "Slack webhook failed for task %s: HTTP %s %s",
                    task.name,
                    resp.status_code,
                    (resp.text or "")[:300],
                )
                return
            for alert in filtered_alerts:
                atype = alert.get("type")
                if atype != "IMMEDIATE":
                    self._mark_alert_sent(
                        task,
                        alert.get("keyword", ""),
                        namespace,
                        pod_name,
                    )
            task.alerts_sent_count = (task.alerts_sent_count or 0) + len(filtered_alerts)
            task_store.save_task(task, ['alerts_sent_count'])
            logger.info(
                "Slack alert sent for task %s (%s/%s): %d alert(s)",
                task.name,
                namespace,
                _pod_deployment_key(pod_name) or pod_name,
                len(filtered_alerts),
            )
        except Exception as e:
            logger.error(f"Failed to send slack alert: {e}")

    def _rotate_and_archive(self, task):
        # 1. Cleanup S3 (if enabled)
        s3_client = self._get_s3_client(task)
        if s3_client:
            self._cleanup_s3(task, s3_client, retention_days=task.retention_days)
        
        task_log_dir = os.path.join(self.LOG_DIR, str(task.id))
        if not os.path.exists(task_log_dir):
            return
            
        files = glob.glob(os.path.join(task_log_dir, "*.log"))
        
        # Determine thresholds
        now = _localtime(_now())
        current_date = now.date()
        current_h_start = (now.hour // 4) * 4
        
        retention_date = current_date - datetime.timedelta(days=max(1, int(task.retention_days or 3)))

        for fp in files:
            fname = os.path.basename(fp)
            # Parse filename
            file_date = None
            file_h_start = -1
            is_4h_chunk = False
            
            try:
                # 1. Try new format with time: ..._YYYY-MM-DD_HHMM.log
                m = re.match(r'.*_(\d{4}-\d{2}-\d{2})_(\d{4})\.log$', fname)
                if m:
                    date_str = m.group(1)
                    time_str = m.group(2)
                    file_date = datetime.date.fromisoformat(date_str)
                    file_h_start = int(time_str[:2])
                    is_4h_chunk = True
                else:
                    # 2. Try error log format: ..._YYYY-MM-DD_error.log
                    m_err = re.match(r'.*_(\d{4}-\d{2}-\d{2})_error\.log$', fname)
                    if m_err:
                        date_str = m_err.group(1)
                        file_date = datetime.date.fromisoformat(date_str)
                        file_h_start = 0 
                    else:
                        # 3. Try standard date format: ..._YYYY-MM-DD.log (covers task_errors_...)
                        m2 = re.match(r'.*_(\d{4}-\d{2}-\d{2})\.log$', fname)
                        if m2:
                            date_str = m2.group(1)
                            file_date = datetime.date.fromisoformat(date_str)
                            file_h_start = 0 # Treat old daily logs as starting at 00:00
            except Exception:
                continue

            if not file_date:
                continue

            # Decisions
            should_delete = False
            should_archive = False
            
            # 1. Retention Check
            if file_date <= retention_date:
                should_delete = True
                should_archive = True # Archive before deleting if enabled?
            
            # 2. Window Completion Check
            # If file is from previous day, or same day but previous window
            if file_date < current_date:
                should_archive = True
                should_delete = True # After archive, we treat it as "moved" to S3
            elif file_date == current_date:
                # Only archive/delete INTRA-DAY if it is explicitly a 4h chunk file
                if is_4h_chunk and file_h_start != -1 and file_h_start < current_h_start:
                    should_archive = True
                    should_delete = True
            
            # Perform Archive
            if should_archive and s3_client:
                # Upload to S3
                # Key: logs/monitor/{task.id}/raw/{namespace}/{pod}/{date}/{fname}
                # But wait, we don't have namespace/pod easily parsed unless we parse 'unique_name'
                # unique_name = namespace_pod
                # But pod name can contain underscores? Yes.
                # So we can't perfectly reconstruct the path expected by S3 Index?
                # The S3 Index expects: indexes/raw/DATE/HH00-HH59.json -> list of files.
                # And the files can be ANYWHERE in the bucket, as long as the key is in the index.
                # So we can choose a simpler structure for archived files.
                # e.g. logs/monitor/{task.id}/archived/{date}/{fname}
                
                s3_key = f"logs/monitor/{task.id}/archived/{file_date.isoformat()}/{fname}"
                
                try:
                    s3_client.upload_file(fp, task.s3_bucket, s3_key)
                    
                    # Record Index Entry!
                    # We need to add this file to the S3 Index so it appears in "History"
                    # Window start for this file:
                    ws_dt = datetime.datetime.combine(file_date, datetime.time(hour=file_h_start, minute=0))
                    ws_dt = _make_aware(ws_dt)
                    
                    fsize = os.path.getsize(fp)
                    mtime = os.path.getmtime(fp)
                    
                    # Log Type? We assume 'raw'. Error logs are separate?
                    # Error logs: ..._error.log.
                    # My regex didn't account for error logs!
                    # "task_errors_..." or "pod_..._error.log"
                    # The fetcher writes error logs too?
                    # _process_log_stream writes to `task_log_dir`?
                    # Yes, `error_file_path = os.path.join(log_dir, f"{source_name}_{today_str}_error.log")`
                    # It uses `_error.log` suffix.
                    
                    ltype = 'error' if 'error.log' in fname else 'raw'
                    
                    # We need to manually invoke _record_index_entry logic, but that function assumes *current* window?
                    # No, _record_index_entry calculates window from 'now'.
                    # But here we are uploading an OLD file. We want it in the OLD index.
                    # I need to modify _record_index_entry or manually update the index for THAT window.
                    
                    # Actually, `_record_index_entry` uses `ws = self._get_4h_window_start(now)`.
                    # I should change it to accept explicit window start?
                    # Or just update `_index_entries` directly here.
                    
                    with self._index_lock:
                        by_task = self._index_entries.setdefault(str(task.id), {})
                        ws_iso = ws_dt.isoformat()
                        by_window = by_task.setdefault(ws_iso, {"raw": {}, "error": {}})
                        by_type = by_window.setdefault(ltype, {})
                        by_type[s3_key] = {"name": s3_key, "size": fsize, "mtime": mtime}
                        # We don't update _index_last_window_start because this might be old
                    
                    # Trigger immediate index flush for this old window?
                    # `_finalize_due_indexes` flushes windows <= latest_completed.
                    # Since this file IS completed, it will be flushed in the next loop.
                    # Perfect.
                    
                except Exception as e:
                    # Check for PermanentRedirect (Region mismatch) during background upload
                    try:
                        import botocore
                        if isinstance(e, botocore.exceptions.ClientError):
                            err_code = e.response.get('Error', {}).get('Code')
                            if err_code in ('301', 'PermanentRedirect'):
                                correct_region = e.response.get('ResponseMetadata', {}).get('HTTPHeaders', {}).get('x-amz-bucket-region')
                                if correct_region and correct_region != task.s3_region:
                                    logger.warning(f"[monitor] Background upload detected S3 Redirect! Updating task {task.id} from {task.s3_region} to {correct_region}")
                                    task.s3_region = correct_region
                                    task_store.save_task(task, ['s3_region'])
                                    # Force client refresh for next file
                                    s3_client = self._get_s3_client(task)
                    except Exception:
                        pass
                        
                    logger.error(f"Failed to upload {fname}: {e}")
                    self._alert_s3_failure(task, str(e), failed_count=1)
                    continue # Don't delete if upload failed
            
            # Perform Delete
            if should_delete:
                # If s3_client missing but should_archive=True (retention), we just delete (data loss but intended by retention)
                # If s3_client present, we only delete if upload succeeded (continue above handles failure)
                try:
                    os.remove(fp)
                except Exception:
                    pass


monitor_engine = MonitorEngine()
