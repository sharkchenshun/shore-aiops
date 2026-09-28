# ============================================================
# app/api/monitor.py — 日志监控 API（合并自 shark-Platform monitor）
# ============================================================

from __future__ import annotations

import datetime
import json
import math
import os
import re
from collections import deque
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_operator, require_user
from app.models.auth import User
from app.models.monitor import MonitorTask
from app.schemas.monitor import BatchSearchRequest, MonitorTaskCreate, MonitorTaskUpdate
from app.services.log_monitor.s3_helpers import (
    get_s3_client,
    handle_s3_error,
    iter_s3_lines,
    list_log_files,
    redact_task,
    resolve_s3_recent_keys,
    task_s3_prefixes,
)
from app.services.log_monitor.engine import _make_aware, _now, monitor_engine

router = APIRouter(prefix="/monitor", tags=["monitor"])


def _safe_local_log_path(log_dir: str, filename: str) -> str | None:
    if not filename or filename in {".", ".."}:
        return None
    if os.path.sep in filename or "/" in filename or "\\" in filename:
        return None
    base = os.path.realpath(log_dir)
    path = os.path.realpath(os.path.join(base, filename))
    if path != base and not path.startswith(base + os.sep):
        return None
    return path if os.path.isfile(path) else None


def _keyword_terms(keyword: str | None) -> list[str] | None:
    """空白关键字不能当成「匹配每一行」。None 表示不做内容搜索。"""
    if not keyword:
        return None
    terms = [k for k in keyword.lower().split() if k]
    return terms or None


def _download_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:180] or "log.log"


async def _get_task(db: AsyncSession, task_id: UUID) -> MonitorTask:
    task = (await db.execute(select(MonitorTask).where(MonitorTask.id == task_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@router.get("/tasks")
async def list_tasks(db: AsyncSession = Depends(get_db), _user: User = Depends(require_user)):
    rows = (await db.execute(select(MonitorTask).order_by(MonitorTask.created_at.desc()))).scalars().all()
    return [redact_task(t) for t in rows]


@router.post("/tasks")
async def create_task(body: MonitorTaskCreate, db: AsyncSession = Depends(get_db), _user: User = Depends(require_operator)):
    task = MonitorTask(**body.model_dump())
    db.add(task)
    await db.flush()
    await db.refresh(task)
    return redact_task(task)


@router.get("/tasks/{task_id}")
async def get_task(task_id: UUID, db: AsyncSession = Depends(get_db), _user: User = Depends(require_user)):
    return redact_task(await _get_task(db, task_id))


@router.put("/tasks/{task_id}")
async def update_task(task_id: UUID, body: MonitorTaskUpdate, db: AsyncSession = Depends(get_db), _user: User = Depends(require_operator)):
    task = await _get_task(db, task_id)
    data = body.model_dump(exclude_unset=True)
    for k in ('s3_access_key', 's3_secret_key', 'k8s_kubeconfig', 'slack_webhook_url'):
        if k in data and not data[k]:
            data.pop(k, None)
    for k, v in data.items():
        setattr(task, k, v)
    await db.flush()
    await db.refresh(task)
    return redact_task(task)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: UUID, db: AsyncSession = Depends(get_db), _user: User = Depends(require_operator)):
    task = await _get_task(db, task_id)
    await db.delete(task)
    return {"msg": "deleted"}


@router.get("/logs")
async def monitor_logs(
    task_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: str = "",
    sort_by: str = "mtime",
    order: str = "desc",
    log_type: str = "all",
    realtime: bool = False,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    task = await _get_task(db, task_id)
    return list_log_files(
        task, page=page, page_size=page_size, search=search.lower(),
        sort_by=sort_by, order=order, log_type=log_type.lower(), realtime=realtime,
    )


@router.get("/logs/history")
async def monitor_logs_history(
    task_id: UUID,
    log_type: str = "raw",
    start: str | None = None,
    end: str | None = None,
    keyword: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    task = await _get_task(db, task_id)
    s3_client = get_s3_client(task)
    if not s3_client:
        return {"items": [], "total": 0}

    lt = log_type if log_type in ('raw', 'error') else 'raw'
    now = _now()
    try:
        start_dt = datetime.datetime.fromisoformat(start) if start else (now - datetime.timedelta(days=7))
    except Exception:
        start_dt = now - datetime.timedelta(days=7)
    try:
        end_dt = datetime.datetime.fromisoformat(end) if end else now
    except Exception:
        end_dt = now
    if start_dt.tzinfo is None:
        start_dt = _make_aware(start_dt)
    if end_dt.tzinfo is None:
        end_dt = _make_aware(end_dt)

    prefix = f"logs/monitor/{task.id}/indexes/{lt}/"
    items = []

    def fetch_items(client):
        fetched = []
        paginator = client.get_paginator('list_objects_v2')
        for p in paginator.paginate(Bucket=task.s3_bucket, Prefix=prefix):
            for obj in p.get('Contents', []) or []:
                if len(fetched) >= 3000:
                    return fetched
                key = obj.get('Key') or ''
                if not key.endswith('.json'):
                    continue
                if keyword and keyword.lower() not in key.lower():
                    continue
                try:
                    parts = key.split('/')
                    date_str = parts[-2]
                    range_str = parts[-1].replace('.json', '')
                    start_h = int(range_str[:2])
                    ws_dt = datetime.datetime.fromisoformat(date_str).replace(
                        hour=start_h, minute=0, second=0, microsecond=0
                    )
                    ws_dt = _make_aware(ws_dt)
                    we_dt = ws_dt + datetime.timedelta(hours=4) - datetime.timedelta(seconds=1)
                except Exception:
                    continue
                if we_dt < start_dt or ws_dt > end_dt:
                    continue
                lm = obj.get('LastModified')
                fetched.append({
                    "key": key,
                    "window_start": ws_dt.isoformat(),
                    "window_end": we_dt.isoformat(),
                    "mtime": lm.timestamp() if lm else 0,
                    "size": int(obj.get('Size') or 0),
                })
        return fetched

    try:
        items = fetch_items(s3_client)
    except Exception as e:
        if handle_s3_error(e, task, lambda t: None):
            s3_client = get_s3_client(task)
            try:
                items = fetch_items(s3_client)
            except Exception as retry_e:
                raise HTTPException(500, f"Retry failed: {retry_e}") from retry_e
        else:
            raise HTTPException(500, str(e)) from e

    items.sort(key=lambda x: x.get('window_start') or '', reverse=True)
    total = len(items)
    start_i = (page - 1) * page_size
    return {"items": items[start_i:start_i + page_size], "total": total, "page": page, "page_size": page_size}


@router.get("/logs/index_detail")
async def monitor_index_detail(task_id: UUID, key: str, db: AsyncSession = Depends(get_db), _user: User = Depends(require_user)):
    task = await _get_task(db, task_id)
    s3_client = get_s3_client(task)
    if not s3_client:
        raise HTTPException(400, "S3 not enabled")
    prefix = f"logs/monitor/{task.id}/indexes/"
    if not key.startswith(prefix):
        raise HTTPException(400, "invalid parameters")
    try:
        obj = s3_client.get_object(Bucket=task.s3_bucket, Key=key)
        return json.loads(obj['Body'].read().decode('utf-8', errors='replace'))
    except Exception as e:
        if handle_s3_error(e, task, lambda t: None):
            s3_client = get_s3_client(task)
            try:
                obj = s3_client.get_object(Bucket=task.s3_bucket, Key=key)
                return json.loads(obj['Body'].read().decode('utf-8', errors='replace'))
            except Exception:
                pass
        raise HTTPException(404, "Index not found") from e


def _serve_local_file(fpath: str, keyword: str | None, page: int, page_size: int, reverse: bool) -> dict:
    max_page = settings.LOG_MONITOR_VIEW_MAX_PAGE_SIZE
    page_size = min(max(1, page_size), max_page)

    keywords = _keyword_terms(keyword)
    if keywords:
        results = []
        max_kw = 2000
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line_lower = line.lower()
                if all(k in line_lower for k in keywords):
                    results.append(line.rstrip())
                    if len(results) >= max_kw:
                        results.append("... (Matches truncated, found > 2000 lines) ...")
                        break
        if reverse:
            results.reverse()
        return {"content": "\n".join(results), "is_search_result": True, "total": len(results)}

    file_size = os.path.getsize(fpath)
    tail_threshold = 5 * 1024 * 1024
    if file_size > tail_threshold or (reverse and file_size > 512 * 1024):
        # 大文件倒序：只读尾部，避免整文件进内存
        read_lines = page_size * max(page, 1) + page_size
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            tail_lines = list(deque(f, read_lines))
        total = len(tail_lines)
        if reverse:
            tail_lines.reverse()
        p = page if page != -1 else 1
        start = (p - 1) * page_size
        chunk = tail_lines[start:start + page_size]
        return {
            "content": "".join(chunk),
            "total": total,
            "page": p,
            "page_size": page_size,
            "warning": "大文件仅加载尾部片段，完整日志请下载。",
            "truncated": file_size > tail_threshold,
        }

    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
        all_lines = f.readlines()
    total = len(all_lines)
    if reverse:
        all_lines.reverse()
    p = page if page != -1 else max(1, math.ceil(total / page_size))
    start = (p - 1) * page_size
    return {"content": "".join(all_lines[start:start + page_size]), "total": total, "page": p, "page_size": page_size}


@router.get("/logs/view")
async def monitor_log_view(
    task_id: UUID,
    filename: str,
    keyword: str | None = None,
    page: int = Query(1, ge=-1, le=100000),
    page_size: int | None = Query(None),
    reverse: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_user),
):
    task = await _get_task(db, task_id)
    if page == 0:
        page = 1
    ps = page_size or settings.LOG_MONITOR_VIEW_PAGE_SIZE
    ps = min(max(1, ps), settings.LOG_MONITOR_VIEW_MAX_PAGE_SIZE)

    if filename.endswith('_s3_recent.log'):
        s3_client = get_s3_client(task)
        target_keys = resolve_s3_recent_keys(task, filename) if s3_client else []
        if s3_client and target_keys:
            keywords = _keyword_terms(keyword)
            if keywords:
                results = []
                for key in target_keys:
                    try:
                        obj = s3_client.get_object(Bucket=task.s3_bucket, Key=key)
                        for line in iter_s3_lines(obj['Body']):
                            ll = line.lower()
                            if all(k in ll for k in keywords):
                                results.append(line.rstrip('\n'))
                                if len(results) > 2000:
                                    break
                    except Exception:
                        pass
                return {"content": "\n".join(results), "is_search_result": True, "total": len(results)}
            full_text_parts: list[str] = []
            remaining = 8 * 1024 * 1024
            for key in target_keys:
                if remaining <= 0:
                    break
                try:
                    obj = s3_client.get_object(Bucket=task.s3_bucket, Key=key)
                    chunk = obj['Body'].read(remaining)
                    full_text_parts.append(chunk.decode('utf-8', errors='replace'))
                    remaining -= len(chunk)
                except Exception:
                    pass
            full_text = "".join(full_text_parts)
            all_lines = full_text.splitlines(True)
            total = len(all_lines)
            if reverse:
                all_lines.reverse()
            p = page if page != -1 else max(1, math.ceil(total / ps))
            start = (p - 1) * ps
            out = {"content": "".join(all_lines[start:start + ps]), "total": total, "page": p, "page_size": ps}
            if remaining <= 0:
                out["warning"] = "S3 实时日志仅加载约 8MB，完整内容请下载或查历史。"
            return out

    log_dir = os.path.join(monitor_engine.LOG_DIR, str(task_id))
    local_path = _safe_local_log_path(log_dir, filename)
    if local_path:
        return _serve_local_file(local_path, keyword, page, ps, reverse)

    s3_client = get_s3_client(task)
    if s3_client and any(filename.startswith(p) for p in task_s3_prefixes(task)):
        try:
            head = s3_client.head_object(Bucket=task.s3_bucket, Key=filename)
            size = int(head.get('ContentLength') or 0)
        except Exception as e:
            if handle_s3_error(e, task, lambda t: None):
                s3_client = get_s3_client(task)
                head = s3_client.head_object(Bucket=task.s3_bucket, Key=filename)
                size = int(head.get('ContentLength') or 0)
            else:
                raise HTTPException(404, "File not found") from e
        keywords = _keyword_terms(keyword)
        if keywords:
            results = []
            obj = s3_client.get_object(Bucket=task.s3_bucket, Key=filename)
            for line in iter_s3_lines(obj['Body']):
                ll = line.lower()
                if all(k in ll for k in keywords):
                    results.append(line.rstrip('\n'))
                    if len(results) > 2000:
                        results.append("... (Matches truncated) ...")
                        break
            return {"content": "\n".join(results), "is_search_result": True, "total": len(results)}
        if size > 50 * 1024 * 1024 and not reverse:
            raise HTTPException(413, "File too large; use reverse=true pagination")
        if size > 5 * 1024 * 1024:
            # S3 大文件：只读尾部
            tail_bytes = min(size, ps * 400 * max(page, 1))
            range_header = f"bytes={max(0, size - tail_bytes)}-{size - 1}"
            obj = s3_client.get_object(Bucket=task.s3_bucket, Key=filename, Range=range_header)
            text = obj['Body'].read().decode('utf-8', errors='replace')
            all_lines = text.splitlines(True)
            total = len(all_lines)
            if reverse:
                all_lines.reverse()
            p = page if page != -1 else 1
            start = (p - 1) * ps
            return {
                "content": "".join(all_lines[start:start + ps]),
                "total": total,
                "page": p,
                "page_size": ps,
                "warning": "S3 大文件仅返回尾部片段",
            }
        obj = s3_client.get_object(Bucket=task.s3_bucket, Key=filename)
        text = obj['Body'].read().decode('utf-8', errors='replace')
        all_lines = text.splitlines(True)
        total = len(all_lines)
        if reverse:
            all_lines.reverse()
        p = page if page != -1 else max(1, math.ceil(total / ps))
        start = (p - 1) * ps
        return {"content": "".join(all_lines[start:start + ps]), "total": total, "page": p, "page_size": ps}

    raise HTTPException(404, "File not found")


@router.get("/logs/download")
async def monitor_log_download(task_id: UUID, filename: str, db: AsyncSession = Depends(get_db), _user: User = Depends(require_user)):
    task = await _get_task(db, task_id)
    log_dir = os.path.join(monitor_engine.LOG_DIR, str(task_id))
    local_path = _safe_local_log_path(log_dir, filename)
    if local_path:
        return FileResponse(local_path, filename=_download_name(filename), media_type='text/plain')

    s3_client = get_s3_client(task)
    if filename.endswith('_s3_recent.log') and s3_client:
        keys = resolve_s3_recent_keys(task, filename)
        if keys:
            def _gen():
                for key in keys:
                    obj = s3_client.get_object(Bucket=task.s3_bucket, Key=key)
                    yield from iter_s3_lines(obj['Body'])
            out_name = _download_name(filename)
            return StreamingResponse(
                _gen(),
                media_type='text/plain; charset=utf-8',
                headers={'Content-Disposition': f'attachment; filename="{out_name}"'},
            )
    if s3_client and any(filename.startswith(p) for p in task_s3_prefixes(task)):
        obj = s3_client.get_object(Bucket=task.s3_bucket, Key=filename)
        out_name = _download_name(os.path.basename(filename) or "log.log")
        return StreamingResponse(
            iter_s3_lines(obj['Body']),
            media_type='text/plain; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{out_name}"'},
        )
    raise HTTPException(404, "File not found")


@router.post("/logs/batch_search")
async def monitor_log_batch_search(body: BatchSearchRequest, db: AsyncSession = Depends(get_db), _user: User = Depends(require_user)):
    task = await _get_task(db, body.task_id)
    s3_client = get_s3_client(task)
    results = []
    max_total = 2000
    keywords = _keyword_terms(body.keyword) or []
    if not keywords:
        return {"results": []}
    log_dir = os.path.join(monitor_engine.LOG_DIR, str(body.task_id))
    prefixes = task_s3_prefixes(task)

    for fname in body.filenames[:50]:
        if len(results) >= max_total:
            break
        is_s3 = bool(s3_client and any(str(fname).startswith(p) for p in prefixes))
        if is_s3:
            try:
                obj = s3_client.get_object(Bucket=task.s3_bucket, Key=fname)
                for i, line in enumerate(iter_s3_lines(obj['Body']), 1):
                    line_lower = line.lower()
                    if all(k in line_lower for k in keywords):
                        results.append({"file": fname, "line": i, "content": line.strip()})
                        if len(results) >= max_total:
                            break
            except Exception:
                pass
        else:
            fpath = _safe_local_log_path(log_dir, str(fname))
            if not fpath:
                continue
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    for i, line in enumerate(f, 1):
                        line_lower = line.lower()
                        if all(k in line_lower for k in keywords):
                            results.append({"file": fname, "line": i, "content": line.strip()})
                            if len(results) >= max_total:
                                break
            except Exception:
                pass
    return {"results": results}


@router.get("/status")
async def monitor_status(_user: User = Depends(require_user)):
    return {"running": monitor_engine.is_running()}
