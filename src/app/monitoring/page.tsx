'use client'

import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  Activity, AlertTriangle, ChevronDown, ChevronRight, Loader2, Play, RefreshCw, Settings2, X,
} from 'lucide-react'
import { apiJson } from '@/lib/api'
import { ErrorState, LoadingSpinner } from '@/components/ui/AsyncState'
import { cn } from '@/lib/utils'
import { useEnvironments } from '@/lib/useEnvironments'

interface PatrolSummary {
  serviceCount: number
  healthSummary: Record<string, number>
  latestPatrol: { output?: { issues?: Issue[]; summary?: Record<string, number>; prometheusConnected?: boolean } } | null
}

interface Issue {
  service: string
  namespace: string
  health: string
  problem: string
  replicas: string
}

interface ReportSummary {
  report_id: string
  timestamp?: string
  score: number | null
  verdict?: string
  findings_count?: number
  summary?: string
}

interface InspectionConfig {
  prometheus_url: string
  ark_base_url: string
  ark_api_key: string
  ark_api_key_set: boolean
  ark_model_id: string
}

interface CheckItem {
  id?: string
  name: string
  level: string
  result: string
  source?: string
  items?: { key?: string; label: string }[]
}

interface InspectionReport {
  report_id: string
  timestamp?: string
  score?: number | null
  verdict?: string
  findings?: string[]
  ai_analysis?: string
  health_summary?: { score?: number | null; level?: string; reasons?: string[] }
  fleet_summary?: {
    server_count?: number
    avg_cpu_pct?: number
    avg_mem_pct?: number
    avg_disk_pct?: number
    hot_count?: number
  }
  alerts_summary?: { firing_total?: number; critical_total?: number; warning_total?: number }
  checklist?: CheckItem[]
  pvc_usage?: { key: string; namespace?: string; pvc?: string; service: string; pct: number; used_bytes: number; capacity_bytes: number; baseline?: string }[]
  workloads?: { key?: string; kind?: string; namespace?: string; name?: string; service: string; desired?: number; ready?: number; level?: string; pods?: { pod: string; phase: string; ready?: boolean | null; waiting?: string; pod_ip?: string; host_ip?: string; node?: string }[] }[]
  elasticsearch?: {
    available?: boolean
    clusters?: { cluster: string; status?: string; nodes?: number; data_nodes?: number; unassigned_shards?: number; active_shards?: number }[]
    heap_nodes?: { cluster: string; node: string; heap_pct: number }[]
  }
  middleware?: { available?: boolean; items?: { id: string; name: string; source: string; level: string; result: string }[] }
  known_normals?: string[]
  decommissioned?: { label?: string; job?: string; instance?: string; when?: string }[]
  servers?: { instance?: string; nodename?: string; role?: string; ip?: string; level?: string; cpu_pct?: number; mem_pct?: number; disk_pct?: number; mem_delta_24h?: number; disk_delta_24h?: number; load1?: number }[]
  busy?: boolean
}

const emptyConfig = (): InspectionConfig => ({
  prometheus_url: '',
  ark_base_url: '',
  ark_api_key: '',
  ark_api_key_set: false,
  ark_model_id: '',
})

function hasScore(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function scoreColor(score: number | null | undefined) {
  if (!hasScore(score)) return 'text-shark-muted'
  if (score >= 80) return 'text-emerald-400'
  if (score >= 60) return 'text-amber-400'
  return 'text-red-400'
}

function levelLabel(level?: string) {
  if (level === 'ok') return '正常'
  if (level === 'warning') return '警告'
  if (level === 'critical') return '严重'
  if (level === 'skip') return '未覆盖'
  return level || '—'
}

function levelClass(level?: string) {
  if (level === 'ok') return 'bg-emerald-500/20 text-emerald-400'
  if (level === 'warning') return 'bg-amber-500/20 text-amber-400'
  if (level === 'critical') return 'bg-red-500/20 text-red-400'
  return 'bg-white/10 text-shark-muted'
}

function fmtPct(v?: number | null) {
  if (v == null || Number.isNaN(v)) return '—'
  return Number(v).toFixed(1)
}

function fmtBytes(n?: number) {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(1)} GB`
}

function fmtDelta(v?: number) {
  if (v == null || Number.isNaN(v)) return '—'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(1)}pt`
}

export default function MonitoringPage() {
  const { active: activeEnv } = useEnvironments()
  const [patrol, setPatrol] = useState<PatrolSummary | null>(null)
  const [reports, setReports] = useState<ReportSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [current, setCurrent] = useState<InspectionReport | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [showConfig, setShowConfig] = useState(false)
  const [config, setConfig] = useState<InspectionConfig>(emptyConfig())
  const [savingConfig, setSavingConfig] = useState(false)
  const [ignores, setIgnores] = useState<{ key: string; label: string; check_id: string }[]>([])
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({
    nodes: false, pvc: true, workloads: true, es: true, middleware: true,
  })

  const fetchAll = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError(null)
    try {
      const [p, r] = await Promise.all([
        apiJson<PatrolSummary>('/api/ops/monitor/summary', { signal }).catch(() => null),
        apiJson<{ items: ReportSummary[] }>('/api/inspection/reports', { signal }),
      ])
      if (signal?.aborted) return
      setPatrol(p)
      setReports(r.items || [])
    } catch (e) {
      if (signal?.aborted) return
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    setCurrent(null)
    const ac = new AbortController()
    fetchAll(ac.signal)
    return () => ac.abort()
  }, [fetchAll, activeEnv])

  const loadConfig = async () => {
    const [cfg, ign] = await Promise.all([
      apiJson<InspectionConfig>('/api/inspection/config'),
      apiJson<{ items: { key: string; label: string; check_id: string }[] }>('/api/inspection/ignores'),
    ])
    setConfig({ ...emptyConfig(), ...cfg, ark_api_key: '' })
    setIgnores(ign.items || [])
    setShowConfig(true)
  }

  const saveConfig = async () => {
    setSavingConfig(true)
    try {
      const payload: Record<string, string> = {
        prometheus_url: config.prometheus_url,
        ark_base_url: config.ark_base_url,
        ark_model_id: config.ark_model_id,
      }
      if (config.ark_api_key && !config.ark_api_key.startsWith('•')) {
        payload.ark_api_key = config.ark_api_key
      }
      await apiJson('/api/inspection/config', { method: 'POST', body: JSON.stringify(payload) })
      setShowConfig(false)
    } catch (e) {
      alert(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSavingConfig(false)
    }
  }

  const runInspection = async () => {
    setRunning(true)
    setError(null)
    try {
      const report = await apiJson<InspectionReport>('/api/inspection/run', {
        method: 'POST',
        body: '{}',
        signal: AbortSignal.timeout(300000),
      })
      setCurrent(report)
      await fetchAll()
    } catch (e) {
      setError(e instanceof Error ? e.message : '巡检失败')
    } finally {
      setRunning(false)
    }
  }

  const viewReport = async (id: string) => {
    setDetailLoading(true)
    try {
      setCurrent(await apiJson<InspectionReport>(`/api/inspection/reports/${id}`))
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取报告失败')
    } finally {
      setDetailLoading(false)
    }
  }

  const ignoreItem = async (key: string, label: string, checkId?: string) => {
    if (!confirm(`忽略「${label || key}」？后续巡检将不再记入发现问题。`)) return
    try {
      await apiJson('/api/inspection/ignores', {
        method: 'POST',
        body: JSON.stringify({ key, label, check_id: checkId || '' }),
      })
      alert('已加入忽略列表')
    } catch (e) {
      alert(e instanceof Error ? e.message : '忽略失败')
    }
  }

  const removeIgnore = async (key: string) => {
    try {
      await apiJson(`/api/inspection/ignores?key=${encodeURIComponent(key)}`, { method: 'DELETE' })
      setIgnores((rows) => rows.filter((r) => r.key !== key))
    } catch (e) {
      alert(e instanceof Error ? e.message : '移除失败')
    }
  }

  const issues = patrol?.latestPatrol?.output?.issues || []
  const summary = patrol?.healthSummary || {}
  const score = current?.health_summary?.score ?? current?.score
  const checklist = (current?.checklist || []).filter((c) => c.level !== 'skip')

  const toggle = (k: string) => setOpenSections((s) => ({ ...s, [k]: !s[k] }))

  return (
    <div className="h-full flex flex-col">
      <header className="shrink-0 glass border-b border-shark-border flex items-center justify-between px-6 h-14">
        <div>
          <h1 className="text-sm font-semibold text-white flex items-center gap-2">
            <Activity size={16} className="text-emerald-400" /> 监控巡检
          </h1>
          <p className="text-[10px] text-shark-muted">
            Prometheus 集群清单 + PVC / 工作负载 / 中间件；环境 {activeEnv}
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => fetchAll()} className="text-xs text-shark-muted hover:text-white flex items-center gap-1 px-2 py-1">
            <RefreshCw size={12} /> 刷新
          </button>
          <button onClick={loadConfig} className="text-xs flex items-center gap-1 px-3 py-1.5 rounded border border-shark-border text-shark-muted hover:text-white">
            <Settings2 size={12} /> 巡检配置
          </button>
          <button onClick={runInspection} disabled={running}
            className="text-xs bg-shark-accent text-white px-3 py-1.5 rounded-lg flex items-center gap-1 disabled:opacity-50">
            {running ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
            立即巡检
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-auto p-6 space-y-6">
        {loading && <LoadingSpinner />}
        {error && <ErrorState message={error} onRetry={() => fetchAll()} />}
        {!loading && (
          <>
            {patrol && (
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                {[
                  ['healthy', '服务健康', 'text-emerald-400'],
                  ['degraded', '降级', 'text-amber-400'],
                  ['critical', '严重', 'text-red-400'],
                  ['unknown', '未知', 'text-shark-muted'],
                  ['total', '服务总数', 'text-white'],
                ].map(([key, label, color]) => (
                  <div key={key} className="glass rounded-xl p-4 border border-shark-border">
                    <div className="text-[10px] text-shark-muted uppercase">{label}</div>
                    <div className={cn('text-2xl font-bold mt-1', color)}>
                      {key === 'total' ? patrol.serviceCount : (summary[key] || 0)}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {patrol?.latestPatrol?.output?.prometheusConnected === false && (
              <p className="text-xs text-amber-400/90">Prometheus 未连通，可在「巡检配置」填写 Endpoint，或在 .env / environments.json 配置 PROMETHEUS_URL。</p>
            )}

            {issues.length > 0 && (
              <section>
                <h2 className="text-xs font-semibold text-shark-muted uppercase mb-3 flex items-center gap-1">
                  <AlertTriangle size={12} /> 服务健康问题 ({issues.length})
                </h2>
                <div className="space-y-2">
                  {issues.slice(0, 8).map((i, idx) => (
                    <div key={idx} className="glass rounded-lg px-4 py-2 border border-shark-border flex items-center gap-4 text-xs">
                      <span className={cn('font-mono', i.health === 'critical' ? 'text-red-400' : 'text-amber-400')}>
                        {i.namespace}/{i.service}
                      </span>
                      <span className="text-shark-muted flex-1">{i.problem}</span>
                      <span className="text-shark-muted">{i.replicas}</span>
                    </div>
                  ))}
                </div>
              </section>
            )}

            <section>
              <h2 className="text-xs font-semibold text-shark-muted uppercase mb-3">巡检报告</h2>
              {reports.length === 0 ? (
                <p className="text-sm text-shark-muted">暂无报告。配置 Prometheus 后点击「立即巡检」。</p>
              ) : (
                <div className="glass rounded-xl border border-shark-border overflow-hidden">
                  <table className="w-full text-xs">
                    <thead className="text-shark-muted bg-white/[0.02]">
                      <tr>
                        <th className="text-left px-4 py-2 font-medium">日期</th>
                        <th className="text-left px-4 py-2 font-medium">健康分</th>
                        <th className="text-left px-4 py-2 font-medium">结论</th>
                        <th className="text-left px-4 py-2 font-medium w-24"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {reports.map((row) => (
                        <tr key={row.report_id} className="border-t border-shark-border/60 hover:bg-white/[0.02]">
                          <td className="px-4 py-3 text-white">{row.report_id}</td>
                          <td className={cn('px-4 py-3 font-semibold', scoreColor(row.score))}>
                            {hasScore(row.score) ? row.score : '未覆盖'}
                          </td>
                          <td className="px-4 py-3 text-shark-muted truncate max-w-xl">{row.summary || row.verdict || '—'}</td>
                          <td className="px-4 py-3">
                            <button onClick={() => viewReport(row.report_id)} className="text-shark-accent hover:underline">
                              查看详情
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </>
        )}
      </div>

      {(current || detailLoading) && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="glass rounded-xl w-full max-w-5xl max-h-[90vh] overflow-hidden flex flex-col">
            <div className="shrink-0 flex items-center justify-between px-5 py-3 border-b border-shark-border">
              <h2 className="text-sm font-semibold text-white">巡检报告 {current?.report_id || ''}</h2>
              <button onClick={() => setCurrent(null)} className="text-shark-muted hover:text-white"><X size={16} /></button>
            </div>
            <div className="flex-1 overflow-auto p-5 space-y-5">
              {detailLoading && <LoadingSpinner />}
              {current && (
                <>
                  {current.busy && <p className="text-xs text-amber-400">上一轮巡检尚未结束，以下为已有报告。</p>}
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                    <Meta label="时间" value={current.timestamp ? new Date(current.timestamp).toLocaleString('zh-CN') : current.report_id} />
                    <Meta label="健康分" value={hasScore(score) ? `${score} / 100` : '—'} valueClass={scoreColor(score)} />
                    <Meta label="节点" value={String(current.fleet_summary?.server_count ?? '—')} />
                    <Meta label="告警" value={String(current.alerts_summary?.firing_total ?? '—')} />
                    <Meta label="结论" value={current.verdict || '—'} />
                  </div>
                  {current.health_summary?.reasons?.length ? (
                    <p className="text-[11px] text-shark-muted">{current.health_summary.reasons.join('；')}</p>
                  ) : null}

                  {(current.findings?.length || checklist.length) ? (
                    <Section title="检查清单">
                      {current.findings && current.findings.length > 0 && (
                        <div className="mb-3">
                          <div className="text-[10px] text-red-400 mb-1">发现问题</div>
                          <ol className="list-decimal pl-4 text-xs text-shark-text space-y-1">
                            {current.findings.map((f, i) => <li key={i}>{f}</li>)}
                          </ol>
                        </div>
                      )}
                      <table className="w-full text-xs">
                        <thead className="text-shark-muted">
                          <tr>
                            <th className="text-left py-1">检查项</th>
                            <th className="text-left py-1 w-20">状态</th>
                            <th className="text-left py-1">结果</th>
                            <th className="text-left py-1 w-16"></th>
                          </tr>
                        </thead>
                        <tbody>
                          {checklist.map((row, idx) => (
                            <tr key={row.id || idx} className="border-t border-shark-border/40 align-top">
                              <td className="py-2 text-white">{row.name}</td>
                              <td className="py-2"><span className={cn('px-1.5 py-0.5 rounded text-[10px]', levelClass(row.level))}>{levelLabel(row.level)}</span></td>
                              <td className="py-2 text-shark-muted">{row.result}</td>
                              <td className="py-2">
                                {(row.level === 'warning' || row.level === 'critical') && row.id && (
                                  <button className="text-shark-accent" onClick={() => ignoreItem(`check:${row.id}`, row.name, row.id)}>忽略</button>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </Section>
                  ) : null}

                  <Collapsible title={`节点资源（${current.servers?.length || 0}）`} open={openSections.nodes} onToggle={() => toggle('nodes')}>
                    <p className="text-[10px] text-shark-muted mb-2">
                      均 CPU {fmtPct(current.fleet_summary?.avg_cpu_pct)}% · 均内存 {fmtPct(current.fleet_summary?.avg_mem_pct)}% · 均磁盘 {fmtPct(current.fleet_summary?.avg_disk_pct)}%
                    </p>
                    <div className="overflow-auto max-h-64">
                      <table className="w-full text-[11px]">
                        <thead className="text-shark-muted">
                          <tr>
                            <th className="text-left py-1">机器</th>
                            <th className="text-left py-1">CPU</th>
                            <th className="text-left py-1">内存</th>
                            <th className="text-left py-1">磁盘</th>
                            <th className="text-left py-1">Δ内存</th>
                            <th className="text-left py-1">Δ磁盘</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(current.servers || []).map((s, i) => (
                            <tr key={s.instance || i} className="border-t border-shark-border/40">
                              <td className="py-1 text-white">{s.nodename || s.instance || '—'}</td>
                              <td className="py-1">{fmtPct(s.cpu_pct)}%</td>
                              <td className="py-1">{fmtPct(s.mem_pct)}%</td>
                              <td className="py-1">{fmtPct(s.disk_pct)}%</td>
                              <td className={cn('py-1', (s.mem_delta_24h || 0) >= 10 ? 'text-amber-400' : '')}>{fmtDelta(s.mem_delta_24h)}</td>
                              <td className={cn('py-1', (s.disk_delta_24h || 0) >= 10 ? 'text-amber-400' : '')}>{fmtDelta(s.disk_delta_24h)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </Collapsible>

                  <Collapsible title={`PVC 用量（${current.pvc_usage?.length || 0}）`} open={openSections.pvc} onToggle={() => toggle('pvc')}>
                    <div className="overflow-auto max-h-72">
                      <table className="w-full text-[11px]">
                        <thead className="text-shark-muted">
                          <tr>
                            <th className="text-left py-1">服务</th>
                            <th className="text-left py-1">PVC</th>
                            <th className="text-left py-1">用量</th>
                            <th className="text-left py-1">容量</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(current.pvc_usage || []).map((p) => (
                            <tr key={p.key} className="border-t border-shark-border/40">
                              <td className="py-1 text-white">{p.service}</td>
                              <td className="py-1 text-shark-muted">{p.namespace}/{p.pvc}</td>
                              <td className={cn('py-1', p.pct >= 95 ? 'text-red-400' : p.pct >= 85 ? 'text-amber-400' : '')}>{fmtPct(p.pct)}%</td>
                              <td className="py-1">{fmtBytes(p.used_bytes)} / {fmtBytes(p.capacity_bytes)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </Collapsible>

                  <Collapsible title={`工作负载（${current.workloads?.length || 0}）`} open={openSections.workloads} onToggle={() => toggle('workloads')}>
                    <div className="overflow-auto max-h-72">
                      <table className="w-full text-[11px]">
                        <thead className="text-shark-muted">
                          <tr>
                            <th className="text-left py-1">工作负载</th>
                            <th className="text-left py-1">服务</th>
                            <th className="text-left py-1">Ready</th>
                            <th className="text-left py-1">状态</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(current.workloads || []).map((w, i) => (
                            <tr key={w.key || i} className="border-t border-shark-border/40">
                              <td className="py-1 text-white">{w.kind} {w.namespace}/{w.name}</td>
                              <td className="py-1">{w.service}</td>
                              <td className="py-1">{w.ready}/{w.desired}</td>
                              <td className="py-1"><span className={cn('px-1.5 py-0.5 rounded text-[10px]', levelClass(w.level))}>{levelLabel(w.level)}</span></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </Collapsible>

                  <Collapsible title="Elasticsearch" open={openSections.es} onToggle={() => toggle('es')}>
                    {(current.elasticsearch?.clusters || []).length === 0 ? (
                      <p className="text-xs text-shark-muted">未扫到 elasticsearch-exporter 指标</p>
                    ) : (
                      <table className="w-full text-[11px]">
                        <thead className="text-shark-muted">
                          <tr>
                            <th className="text-left py-1">集群</th>
                            <th className="text-left py-1">状态</th>
                            <th className="text-left py-1">节点</th>
                            <th className="text-left py-1">未分配分片</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(current.elasticsearch?.clusters || []).map((c) => (
                            <tr key={c.cluster} className="border-t border-shark-border/40">
                              <td className="py-1 text-white">{c.cluster}</td>
                              <td className="py-1">{c.status}</td>
                              <td className="py-1">{c.nodes} / data {c.data_nodes}</td>
                              <td className="py-1">{c.unassigned_shards}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </Collapsible>

                  <Collapsible title={`中间件（${current.middleware?.items?.length || 0}）`} open={openSections.middleware} onToggle={() => toggle('middleware')}>
                    <table className="w-full text-[11px]">
                      <thead className="text-shark-muted">
                        <tr>
                          <th className="text-left py-1">名称</th>
                          <th className="text-left py-1">来源</th>
                          <th className="text-left py-1">状态</th>
                          <th className="text-left py-1">结果</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(current.middleware?.items || []).map((m) => (
                          <tr key={m.id} className="border-t border-shark-border/40">
                            <td className="py-1 text-white">{m.name}</td>
                            <td className="py-1 text-shark-muted">{m.source}</td>
                            <td className="py-1"><span className={cn('px-1.5 py-0.5 rounded text-[10px]', levelClass(m.level))}>{levelLabel(m.level)}</span></td>
                            <td className="py-1">{m.result}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </Collapsible>

                  {!!current.known_normals?.length && (
                    <Section title="已知常态">
                      <ul className="list-disc pl-4 text-xs text-shark-muted space-y-1">
                        {current.known_normals.map((n, i) => <li key={i}>{n}</li>)}
                      </ul>
                    </Section>
                  )}

                  {!!current.decommissioned?.length && (
                    <Section title="已下线残留">
                      <ul className="list-disc pl-4 text-xs text-amber-400/90 space-y-1">
                        {current.decommissioned.map((d, i) => (
                          <li key={i}>{d.label || `${d.job} ${d.instance}`} {d.when ? `· ${d.when}` : ''}</li>
                        ))}
                      </ul>
                    </Section>
                  )}

                  <Section title="AI 分析">
                    <pre className="text-xs text-shark-text whitespace-pre-wrap leading-relaxed">
                      {current.ai_analysis || '未配置 LLM，或本次未生成分析。可在巡检配置填写模型，或使用平台 LLM_* 环境变量。'}
                    </pre>
                  </Section>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {showConfig && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="glass rounded-xl w-full max-w-lg max-h-[85vh] overflow-auto p-5 space-y-4">
            <h2 className="text-sm font-semibold text-white">巡检配置</h2>
            <Field label="Prometheus URL">
              <input value={config.prometheus_url} onChange={(e) => setConfig({ ...config, prometheus_url: e.target.value })} className="input-field" placeholder="http://prometheus:9090" />
            </Field>
            <Field label="LLM Base URL">
              <input value={config.ark_base_url} onChange={(e) => setConfig({ ...config, ark_base_url: e.target.value })} className="input-field" placeholder="留空则用平台 LLM_BASE_URL" />
            </Field>
            <Field label="LLM API Key">
              <input
                value={config.ark_api_key}
                onChange={(e) => setConfig({ ...config, ark_api_key: e.target.value })}
                className="input-field"
                placeholder={
                  config.ark_base_url
                    ? (config.ark_api_key_set ? '已保存密钥：留空表示不修改' : '使用自定义 Base URL 时必须填写密钥')
                    : (config.ark_api_key_set ? '已保存密钥：留空表示不修改' : '可选，留空则用平台 LLM_API_KEY')
                }
              />
            </Field>
            <Field label="LLM Model">
              <input value={config.ark_model_id} onChange={(e) => setConfig({ ...config, ark_model_id: e.target.value })} className="input-field" placeholder="留空则用平台 LLM_MODEL" />
            </Field>
            {ignores.length > 0 && (
              <div>
                <div className="text-[10px] text-shark-muted mb-2">忽略项</div>
                <div className="space-y-1 max-h-40 overflow-auto">
                  {ignores.map((ig) => (
                    <div key={ig.key} className="flex items-center justify-between text-xs border border-shark-border rounded px-2 py-1">
                      <span className="truncate text-shark-text">{ig.label || ig.key}</span>
                      <button onClick={() => removeIgnore(ig.key)} className="text-red-400 shrink-0 ml-2">移除</button>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="flex justify-end gap-2 pt-2">
              <button onClick={() => setShowConfig(false)} className="text-xs px-4 py-2 rounded border border-shark-border text-shark-muted">取消</button>
              <button onClick={saveConfig} disabled={savingConfig} className="text-xs px-4 py-2 rounded bg-shark-accent text-white disabled:opacity-50">
                {savingConfig ? '保存中...' : '保存'}
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

function Meta({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="glass rounded-lg p-3 border border-shark-border">
      <div className="text-[10px] text-shark-muted uppercase">{label}</div>
      <div className={cn('text-sm mt-1 truncate', valueClass || 'text-white')}>{value}</div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="text-xs font-semibold text-white mb-2">{title}</h3>
      {children}
    </section>
  )
}

function Collapsible({ title, open, onToggle, children }: { title: string; open: boolean; onToggle: () => void; children: ReactNode }) {
  return (
    <section>
      <button type="button" onClick={onToggle} className="flex items-center gap-1 text-xs font-semibold text-white mb-2">
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        {title}
      </button>
      {open && children}
    </section>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-[10px] text-shark-muted">{label}</span>
      {children}
    </label>
  )
}
