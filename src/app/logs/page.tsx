'use client'

import { Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import {
  FileText, Loader2, Plus, RefreshCw, Search, Settings2, Trash2, WifiOff, ChevronLeft, ChevronRight,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { apiJson } from '@/lib/api'
import { useEnvironments } from '@/lib/useEnvironments'

const FILE_PAGE_SIZE = 30
const CONTENT_PAGE_SIZE = 500

interface MonitorTask {
  id: string
  name: string
  enabled: boolean
  environment_id?: string
  k8s_namespace: string
  poll_interval_seconds: number
  alert_enabled: boolean
  s3_archive_enabled: boolean
  last_run?: string | null
  last_error?: string | null
  alerts_sent_count: number
  alert_keywords: string[]
  immediate_keywords: string[]
  ignore_keywords: string[]
  record_only_keywords: string[]
  alert_threshold_count: number
  alert_threshold_window: number
  alert_silence_minutes: number
  slack_webhook_url?: string | null
  slack_webhook_set?: boolean
  retention_days: number
}

interface LogFile {
  name: string
  size?: number
  mtime?: number
  is_virtual?: boolean
}

const emptyTask = (envId = 'test'): Partial<MonitorTask> => ({
  name: 'New Monitor',
  enabled: false,
  environment_id: envId,
  k8s_namespace: 'default',
  poll_interval_seconds: 60,
  alert_enabled: true,
  s3_archive_enabled: false,
  alert_keywords: ['error', 'exception'],
  immediate_keywords: [],
  ignore_keywords: [],
  record_only_keywords: [],
  alert_threshold_count: 1,
  alert_threshold_window: 60,
  alert_silence_minutes: 60,
  retention_days: 3,
})

function fmtSize(n?: number) {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function fmtTime(ts?: number | string | null) {
  if (!ts) return '—'
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  return d.toLocaleString('zh-CN')
}

function linesToText(v: string[] | string | undefined) {
  if (!v) return ''
  return Array.isArray(v) ? v.join('\n') : v
}

function textToLines(v: string) {
  return v.split('\n').map((s) => s.trim()).filter(Boolean)
}

export default function LogsPage() {
  return (
    <Suspense fallback={
      <div className="h-full flex items-center justify-center">
        <Loader2 size={32} className="animate-spin text-shark-accent" />
      </div>
    }>
      <LogsPageContent />
    </Suspense>
  )
}

function LogsPageContent() {
  const searchParams = useSearchParams()
  const { active: activeEnv } = useEnvironments()
  const [tasks, setTasks] = useState<MonitorTask[]>([])
  const [showAllEnvs, setShowAllEnvs] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<MonitorTask | null>(null)
  const [files, setFiles] = useState<LogFile[]>([])
  const [fileTotal, setFileTotal] = useState(0)
  const [filePage, setFilePage] = useState(1)
  const [fileSearch, setFileSearch] = useState('')
  const [logType, setLogType] = useState<'all' | 'raw' | 'error'>('all')
  const [content, setContent] = useState('')
  const [contentLoading, setContentLoading] = useState(false)
  const [contentPage, setContentPage] = useState(1)
  const [contentTotal, setContentTotal] = useState(0)
  const [contentWarning, setContentWarning] = useState<string | null>(null)
  const [activeFile, setActiveFile] = useState<string | null>(null)
  const [keyword, setKeyword] = useState('')
  const [showEditor, setShowEditor] = useState(false)
  const [draft, setDraft] = useState<Partial<MonitorTask>>(emptyTask())
  const [saving, setSaving] = useState(false)

  const visibleTasks = useMemo(() => {
    if (showAllEnvs) return tasks
    const env = activeEnv || 'test'
    return tasks.filter((t) => (t.environment_id || 'test') === env)
  }, [tasks, activeEnv, showAllEnvs])

  const loadTasks = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await apiJson<MonitorTask[]>('/api/monitor/tasks')
      setTasks(Array.isArray(data) ? data : [])
    } catch (e) {
      setError(e instanceof Error ? e.message : '无法连接 API')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadFiles = useCallback(async (task: MonitorTask, page = filePage, search = fileSearch) => {
    try {
      const params = new URLSearchParams({
        task_id: task.id,
        page: String(page),
        page_size: String(FILE_PAGE_SIZE),
        log_type: logType,
        realtime: 'true',
        sort_by: 'mtime',
        order: 'desc',
      })
      if (search) params.set('search', search)
      const data = await apiJson<{ files: LogFile[]; total: number }>(`/api/monitor/logs?${params}`)
      setFiles(data.files || [])
      setFileTotal(data.total || 0)
      setFilePage(page)
    } catch {
      setFiles([])
      setFileTotal(0)
    }
  }, [fileSearch, logType, filePage])

  const viewLog = useCallback(async (task: MonitorTask, filename: string, page = 1, kw?: string) => {
    setContentLoading(true)
    setActiveFile(filename)
    setContentPage(page)
    try {
      const params = new URLSearchParams({
        task_id: task.id,
        filename,
        page: String(page),
        page_size: String(CONTENT_PAGE_SIZE),
        reverse: 'true',
      })
      if (kw) params.set('keyword', kw)
      const data = await apiJson<{
        content?: string
        error?: string
        total?: number
        warning?: string
      }>(`/api/monitor/logs/view?${params}`)
      if (data.error) throw new Error(data.error)
      setContent(data.content || '')
      setContentTotal(data.total || 0)
      setContentWarning(data.warning || null)
    } catch (e) {
      setContent(e instanceof Error ? e.message : '读取失败')
      setContentTotal(0)
      setContentWarning(null)
    } finally {
      setContentLoading(false)
    }
  }, [])

  useEffect(() => { loadTasks() }, [loadTasks, activeEnv])

  useEffect(() => {
    const taskId = searchParams.get('taskId')
    const filename = searchParams.get('filename')
    if (!taskId || tasks.length === 0) return
    const task = tasks.find((t) => t.id === taskId)
    if (!task) return
    setSelected(task)
    loadFiles(task, 1).then(() => {
      if (filename) viewLog(task, filename, 1)
    })
  }, [searchParams, tasks, loadFiles, viewLog])

  const selectTask = async (task: MonitorTask) => {
    setSelected(task)
    setContent('')
    setActiveFile(null)
    setFilePage(1)
    setContentPage(1)
    await loadFiles(task, 1)
  }

  const openCreate = () => {
    setDraft(emptyTask(activeEnv || 'test'))
    setShowEditor(true)
  }

  const openEdit = (task: MonitorTask) => {
    setDraft({ ...task })
    setShowEditor(true)
  }

  const saveTask = async () => {
    setSaving(true)
    try {
      const payload = {
        ...draft,
        alert_keywords: textToLines(linesToText(draft.alert_keywords as string[] | string)),
        immediate_keywords: textToLines(linesToText(draft.immediate_keywords as string[] | string)),
        ignore_keywords: textToLines(linesToText(draft.ignore_keywords as string[] | string)),
        record_only_keywords: textToLines(linesToText(draft.record_only_keywords as string[] | string)),
      }
      const isEdit = Boolean(draft.id)
      await apiJson(
        isEdit ? `/api/monitor/tasks/${draft.id}` : '/api/monitor/tasks',
        {
          method: isEdit ? 'PUT' : 'POST',
          body: JSON.stringify(payload),
        }
      )
      setShowEditor(false)
      await loadTasks()
    } catch (e) {
      alert(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const deleteTask = async (task: MonitorTask) => {
    if (!confirm(`删除监控任务「${task.name}」？`)) return
    await apiJson(`/api/monitor/tasks/${task.id}`, { method: 'DELETE' })
    if (selected?.id === task.id) {
      setSelected(null)
      setFiles([])
      setContent('')
    }
    await loadTasks()
  }

  const contentTotalPages = Math.max(1, Math.ceil(contentTotal / CONTENT_PAGE_SIZE))
  const fileTotalPages = Math.max(1, Math.ceil(fileTotal / FILE_PAGE_SIZE))

  const activeStats = useMemo(() => ({
    enabled: visibleTasks.filter((t) => t.enabled).length,
    alerts: visibleTasks.reduce((s, t) => s + (t.alerts_sent_count || 0), 0),
  }), [visibleTasks])

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 size={32} className="animate-spin text-shark-accent" />
      </div>
    )
  }

  if (error && tasks.length === 0) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center space-y-3">
          <WifiOff size={40} className="text-red-400 mx-auto" />
          <p className="text-sm text-shark-muted">{error}</p>
          <button onClick={loadTasks} className="text-xs text-shark-accent border border-shark-accent/30 px-3 py-1.5 rounded">重试</button>
        </div>
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col">
      <header className="shrink-0 glass border-b border-shark-border flex items-center px-6 h-14 gap-3">
        <h1 className="text-sm font-semibold text-white flex items-center gap-2">
          <FileText size={16} className="text-emerald-400" /> 日志告警
        </h1>
        <span className="text-xs text-shark-muted">
          {visibleTasks.length} 任务 · {activeStats.enabled} 启用 · 环境 {activeEnv || 'test'}
        </span>
        <label className="ml-2 text-[10px] text-shark-muted flex items-center gap-1 cursor-pointer">
          <input type="checkbox" checked={showAllEnvs} onChange={(e) => setShowAllEnvs(e.target.checked)} />
          显示全部环境
        </label>
        <div className="ml-auto flex items-center gap-2">
          <button onClick={openCreate} className="text-xs flex items-center gap-1 px-3 py-1.5 rounded bg-shark-accent/20 text-shark-accent border border-shark-accent/30 hover:bg-shark-accent/30">
            <Plus size={14} /> 新建任务
          </button>
          <button onClick={loadTasks} className="text-xs text-shark-muted hover:text-white flex items-center gap-1">
            <RefreshCw size={14} /> 刷新
          </button>
        </div>
      </header>

      <div className="flex-1 flex overflow-hidden">
        <div className="w-[300px] border-r border-shark-border overflow-auto">
          {visibleTasks.length === 0 && (
            <p className="text-xs text-shark-muted p-4">当前环境暂无任务{showAllEnvs ? '' : '，可勾选「显示全部环境」或新建'}</p>
          )}
          {visibleTasks.map((t) => (
            <div
              key={t.id}
              className={cn(
                'p-4 border-b border-shark-border cursor-pointer transition-colors',
                selected?.id === t.id ? 'bg-shark-accent/10 border-l-2 border-l-shark-accent' : 'hover:bg-white/[0.02]'
              )}
              onClick={() => selectTask(t)}
            >
              <div className="flex items-center justify-between mb-1 gap-1">
                <span className="text-sm font-medium text-white truncate">{t.name}</span>
                <span className={cn('text-[10px] px-1.5 py-0.5 rounded shrink-0', t.enabled ? 'bg-emerald-500/20 text-emerald-400' : 'bg-shark-muted/20 text-shark-muted')}>
                  {t.enabled ? '启用' : '停用'}
                </span>
              </div>
              <p className="text-[10px] text-shark-muted mb-1">
                <span className="text-shark-accent uppercase">{t.environment_id || 'test'}</span>
                {' · '}{t.k8s_namespace} · {t.poll_interval_seconds}s
              </p>
              {t.last_error && <p className="text-[10px] text-red-400 truncate">{t.last_error}</p>}
              <div className="flex gap-1 mt-2">
                <button onClick={(e) => { e.stopPropagation(); openEdit(t) }} className="text-[10px] px-2 py-0.5 rounded border border-shark-border text-shark-muted hover:text-white">
                  <Settings2 size={10} className="inline mr-1" />配置
                </button>
                <button onClick={(e) => { e.stopPropagation(); deleteTask(t) }} className="text-[10px] px-2 py-0.5 rounded border border-red-500/30 text-red-400 hover:bg-red-500/10">
                  <Trash2 size={10} className="inline mr-1" />删除
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="flex-1 flex flex-col overflow-hidden">
          {!selected ? (
            <div className="flex-1 flex items-center justify-center text-shark-muted text-sm">选择左侧任务查看日志（最新在前）</div>
          ) : (
            <>
              <div className="shrink-0 p-4 border-b border-shark-border space-y-3">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm text-white font-medium">{selected.name}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-shark-accent/20 text-shark-accent uppercase">{selected.environment_id || 'test'}</span>
                  <span className="text-[10px] text-shark-muted">上次运行 {fmtTime(selected.last_run)}</span>
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                  <div className="relative flex-1 max-w-xs">
                    <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-shark-muted" />
                    <input
                      value={fileSearch}
                      onChange={(e) => setFileSearch(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && loadFiles(selected, 1, fileSearch)}
                      placeholder="搜索文件名..."
                      className="w-full pl-8 pr-3 py-1.5 text-xs rounded-lg bg-shark-card border border-shark-border text-white"
                    />
                  </div>
                  <select
                    value={logType}
                    onChange={(e) => setLogType(e.target.value as 'all' | 'raw' | 'error')}
                    className="text-xs px-2 py-1.5 rounded-lg bg-shark-card border border-shark-border text-white"
                  >
                    <option value="all">全部</option>
                    <option value="raw">原始</option>
                    <option value="error">错误</option>
                  </select>
                  <button onClick={() => loadFiles(selected, 1)} className="text-xs px-3 py-1.5 rounded border border-shark-border text-shark-muted hover:text-white">查询</button>
                  <input
                    value={keyword}
                    onChange={(e) => setKeyword(e.target.value)}
                    placeholder="内容关键词..."
                    className="text-xs px-3 py-1.5 rounded-lg bg-shark-card border border-shark-border text-white w-36"
                  />
                  {activeFile && keyword && (
                    <button onClick={() => viewLog(selected, activeFile, 1, keyword)} className="text-xs px-3 py-1.5 rounded bg-shark-accent/20 text-shark-accent">搜索内容</button>
                  )}
                  <span className="text-[10px] text-shark-muted ml-auto">{fileTotal} 个文件</span>
                </div>
              </div>

              <div className="flex-1 flex overflow-hidden">
                <div className="w-64 border-r border-shark-border flex flex-col">
                  <div className="flex-1 overflow-auto">
                    {files.length === 0 && <p className="text-xs text-shark-muted p-3">暂无日志文件</p>}
                    {files.map((f) => (
                      <button
                        key={f.name}
                        onClick={() => viewLog(selected, f.name, 1)}
                        className={cn(
                          'w-full text-left px-3 py-2 border-b border-shark-border/50 hover:bg-white/[0.03]',
                          activeFile === f.name && 'bg-shark-accent/10'
                        )}
                      >
                        <p className="text-xs text-white truncate">{f.name}</p>
                        <p className="text-[10px] text-shark-muted">{fmtSize(f.size)} · {fmtTime(f.mtime)}</p>
                      </button>
                    ))}
                  </div>
                  {fileTotalPages > 1 && (
                    <div className="shrink-0 flex items-center justify-between px-2 py-2 border-t border-shark-border text-[10px] text-shark-muted">
                      <button
                        disabled={filePage <= 1}
                        onClick={() => loadFiles(selected, filePage - 1)}
                        className="p-1 disabled:opacity-30 hover:text-white"
                      >
                        <ChevronLeft size={14} />
                      </button>
                      <span>{filePage}/{fileTotalPages}</span>
                      <button
                        disabled={filePage >= fileTotalPages}
                        onClick={() => loadFiles(selected, filePage + 1)}
                        className="p-1 disabled:opacity-30 hover:text-white"
                      >
                        <ChevronRight size={14} />
                      </button>
                    </div>
                  )}
                </div>
                <div className="flex-1 flex flex-col overflow-hidden">
                  <div className="shrink-0 flex items-center justify-between px-4 py-2 border-b border-shark-border/50 text-[10px] text-shark-muted">
                    <span>{activeFile ? `${activeFile} · 倒序 · 共 ${contentTotal} 行` : '选择文件'}</span>
                    {activeFile && contentTotalPages > 1 && (
                      <div className="flex items-center gap-2">
                        <button
                          disabled={contentPage <= 1 || contentLoading}
                          onClick={() => selected && activeFile && viewLog(selected, activeFile, contentPage - 1, keyword || undefined)}
                          className="p-1 disabled:opacity-30 hover:text-white"
                        >
                          <ChevronLeft size={14} />
                        </button>
                        <span>第 {contentPage}/{contentTotalPages} 页</span>
                        <button
                          disabled={contentPage >= contentTotalPages || contentLoading}
                          onClick={() => selected && activeFile && viewLog(selected, activeFile, contentPage + 1, keyword || undefined)}
                          className="p-1 disabled:opacity-30 hover:text-white"
                        >
                          <ChevronRight size={14} />
                        </button>
                      </div>
                    )}
                  </div>
                  {contentWarning && (
                    <p className="shrink-0 px-4 py-1 text-[10px] text-amber-400 bg-amber-500/10">{contentWarning}</p>
                  )}
                  <div className="flex-1 overflow-auto p-4">
                    {contentLoading ? (
                      <Loader2 size={24} className="animate-spin text-shark-accent mx-auto mt-8" />
                    ) : (
                      <pre className="text-[11px] font-mono text-shark-text whitespace-pre-wrap leading-relaxed">
                        {content || (activeFile ? '空文件' : '选择文件查看内容（最新日志在前）')}
                      </pre>
                    )}
                  </div>
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {showEditor && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="glass rounded-xl w-full max-w-lg max-h-[85vh] overflow-auto p-5 space-y-4">
            <h2 className="text-sm font-semibold text-white">{draft.id ? '编辑任务' : '新建监控任务'}</h2>
            <Field label="名称"><input value={draft.name || ''} onChange={(e) => setDraft({ ...draft, name: e.target.value })} className="input-field" /></Field>
            <Field label="环境">
              <select value={draft.environment_id || 'test'} onChange={(e) => setDraft({ ...draft, environment_id: e.target.value })} className="input-field">
                <option value="dev">dev（K8s context: dev）</option>
                <option value="test">test（K8s context: test）</option>
              </select>
            </Field>
            <Field label="K8s 命名空间（逗号分隔）"><input value={draft.k8s_namespace || ''} onChange={(e) => setDraft({ ...draft, k8s_namespace: e.target.value })} className="input-field" /></Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="轮询间隔(秒)"><input type="number" value={draft.poll_interval_seconds || 60} onChange={(e) => setDraft({ ...draft, poll_interval_seconds: +e.target.value })} className="input-field" /></Field>
              <Field label="保留天数"><input type="number" value={draft.retention_days || 3} onChange={(e) => setDraft({ ...draft, retention_days: +e.target.value })} className="input-field" /></Field>
            </div>
            <label className="flex items-center gap-2 text-xs text-white">
              <input type="checkbox" checked={!!draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> 启用监控
            </label>
            <label className="flex items-center gap-2 text-xs text-white">
              <input type="checkbox" checked={!!draft.alert_enabled} onChange={(e) => setDraft({ ...draft, alert_enabled: e.target.checked })} /> 启用告警
            </label>
            <Field label="Slack Webhook">
              <input
                value={draft.slack_webhook_url || (draft.slack_webhook_set ? '••••••••（已配置，输入新 URL 可覆盖）' : '')}
                onChange={(e) => setDraft({ ...draft, slack_webhook_url: e.target.value.startsWith('••') ? '' : e.target.value })}
                className="input-field"
                placeholder="https://hooks.slack.com/..."
              />
            </Field>
            <Field label="即时告警关键词（每行一个）"><textarea rows={2} value={linesToText(draft.immediate_keywords as string[])} onChange={(e) => setDraft({ ...draft, immediate_keywords: e.target.value.split('\n') })} className="input-field" /></Field>
            <Field label="阈值告警关键词"><textarea rows={2} value={linesToText(draft.alert_keywords as string[])} onChange={(e) => setDraft({ ...draft, alert_keywords: e.target.value.split('\n') })} className="input-field" /></Field>
            <div className="grid grid-cols-3 gap-2">
              <Field label="阈值次数"><input type="number" value={draft.alert_threshold_count ?? 1} onChange={(e) => setDraft({ ...draft, alert_threshold_count: +e.target.value })} className="input-field" /></Field>
              <Field label="窗口(秒)"><input type="number" value={draft.alert_threshold_window || 60} onChange={(e) => setDraft({ ...draft, alert_threshold_window: +e.target.value })} className="input-field" /></Field>
              <Field label="静默(分)"><input type="number" value={draft.alert_silence_minutes || 60} onChange={(e) => setDraft({ ...draft, alert_silence_minutes: +e.target.value })} className="input-field" /></Field>
            </div>
            <Field label="忽略关键词"><textarea rows={2} value={linesToText(draft.ignore_keywords as string[])} onChange={(e) => setDraft({ ...draft, ignore_keywords: e.target.value.split('\n') })} className="input-field" /></Field>
            <Field label="仅记录关键词"><textarea rows={2} value={linesToText(draft.record_only_keywords as string[])} onChange={(e) => setDraft({ ...draft, record_only_keywords: e.target.value.split('\n') })} className="input-field" /></Field>
            <div className="flex justify-end gap-2 pt-2">
              <button onClick={() => setShowEditor(false)} className="text-xs px-4 py-2 rounded border border-shark-border text-shark-muted">取消</button>
              <button onClick={saveTask} disabled={saving} className="text-xs px-4 py-2 rounded bg-shark-accent text-white disabled:opacity-50">
                {saving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      <style jsx global>{`
        .input-field {
          width: 100%;
          padding: 0.375rem 0.625rem;
          font-size: 0.75rem;
          border-radius: 0.5rem;
          background: rgba(255,255,255,0.03);
          border: 1px solid rgba(255,255,255,0.08);
          color: white;
        }
      `}</style>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-[10px] text-shark-muted">{label}</span>
      {children}
    </label>
  )
}
