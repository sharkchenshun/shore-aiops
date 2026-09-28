'use client'

import { useCallback, useEffect, useState } from 'react'
import { Settings, CheckCircle2, XCircle, AlertCircle, RefreshCw, Loader2 } from 'lucide-react'
import { apiJson } from '@/lib/api'
import { ErrorState } from '@/components/ui/AsyncState'
import Link from 'next/link'
import { useEnvironments } from '@/lib/useEnvironments'

type CheckStatus = 'ok' | 'error' | 'not_configured' | 'unavailable' | 'empty' | 'degraded'

interface IntegrationChecks {
  database?: { status: CheckStatus; detail?: string }
  prometheus?: { status: CheckStatus; url?: string; detail?: string }
  elasticsearch?: { status: CheckStatus; host?: string; detail?: string }
  llm?: { status: CheckStatus; model?: string; base_url?: string }
  redis?: { status: CheckStatus }
  gitlab?: { status: CheckStatus; url?: string; detail?: string }
  argocd?: { status?: CheckStatus; environments?: Array<{ env: string; server: string; status: string; detail?: string }> }
  alertmanager?: { webhook: string; token_required: boolean }
  environment?: string
}

function StatusIcon({ status }: { status?: CheckStatus }) {
  if (status === 'ok') return <CheckCircle2 size={16} className="text-emerald-400" />
  if (status === 'not_configured') return <AlertCircle size={16} className="text-amber-400" />
  if (status === 'degraded' || status === 'unavailable') return <AlertCircle size={16} className="text-amber-400" />
  return <XCircle size={16} className="text-red-400" />
}

function KubeconfigEditor({ environments }: { environments: Array<{ id: string; label: string; clusterName: string; kubeconfigConfigured?: boolean; kubeconfigStatus?: string; kubeconfigDetail?: string }> }) {
  const [selected, setSelected] = useState('')
  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    if (!selected && environments.length) setSelected(environments[0].id)
  }, [environments, selected])

  const save = async () => {
    if (!selected || !text.trim()) return
    setSaving(true)
    setMsg('')
    try {
      await apiJson(`/api/settings/environments/${selected}/kubeconfig`, {
        method: 'PUT',
        body: JSON.stringify({ kubeconfig: text }),
      })
      setMsg('已保存')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  if (!environments.length) return null
  const cur = environments.find(e => e.id === selected)
  const authOk = cur?.kubeconfigStatus === 'ok'
  const authErr = cur?.kubeconfigStatus === 'error'

  return (
    <div className="space-y-3">
      {cur && (
        <div className={`text-xs rounded-lg px-3 py-2 border ${authOk ? 'border-emerald-500/30 text-emerald-300 bg-emerald-500/10' : authErr ? 'border-red-500/30 text-red-300 bg-red-500/10' : 'border-amber-500/30 text-amber-300 bg-amber-500/10'}`}>
          {authOk ? '✓ K8s 连接正常' : authErr ? `✗ ${cur.kubeconfigDetail || 'K8s 认证失败'}` : (cur.kubeconfigConfigured ? '未检测连接' : '未配置 kubeconfig')}
          {authErr && cur.kubeconfigDetail?.includes('过期') && (
            <p className="mt-1 text-shark-muted">请向集群管理员重新签发证书，粘贴<strong>新的</strong> kubeconfig（不是同一份旧文件）。</p>
          )}
        </div>
      )}
      <div className="flex items-center gap-3">
        <select
          value={selected}
          onChange={e => setSelected(e.target.value)}
          className="bg-shark-bg border border-shark-border rounded-lg px-3 py-2 text-sm text-white"
        >
          {environments.map(env => (
            <option key={env.id} value={env.id}>
              {env.label} ({env.clusterName}){env.kubeconfigConfigured ? ' ✓' : ''}
            </option>
          ))}
        </select>
        <button
          onClick={save}
          disabled={saving || !text.trim()}
          className="text-xs px-3 py-2 rounded bg-shark-accent text-white disabled:opacity-50"
        >
          {saving ? '保存中…' : '保存'}
        </button>
        {msg && <span className="text-xs text-shark-muted">{msg}</span>}
      </div>
      <textarea
        value={text}
        onChange={e => setText(e.target.value)}
        placeholder={cur ? `粘贴 ${cur.label} 集群 kubeconfig…` : '粘贴 kubeconfig…'}
        rows={8}
        className="w-full bg-slate-950/70 border border-shark-border rounded-lg px-3 py-2 text-xs text-white font-mono"
      />
    </div>
  )
}

export default function SettingsPage() {
  const { environments, active, switchEnv } = useEnvironments()
  const [checks, setChecks] = useState<IntegrationChecks | null>(null)
  const [maintenance, setMaintenance] = useState<Array<{ service: string; reason: string; until: number }>>([])
  const [checksLoading, setChecksLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [svc, setSvc] = useState('')
  const [mins, setMins] = useState(60)

  const loadMaintenance = useCallback(async () => {
    const m = await apiJson<{ windows: typeof maintenance }>('/api/settings/maintenance')
    setMaintenance(m.windows || [])
  }, [])

  const loadChecks = useCallback(async (refresh = false) => {
    setChecksLoading(true)
    setError(null)
    try {
      const q = refresh ? '?refresh=true' : ''
      setChecks(await apiJson<IntegrationChecks>(`/api/settings/integrations${q}`))
    } catch (e) {
      setError(e instanceof Error ? e.message : '健康检查失败')
    } finally {
      setChecksLoading(false)
    }
  }, [])

  useEffect(() => {
    loadMaintenance().catch(e => setError(e instanceof Error ? e.message : '加载失败'))
    loadChecks()
  }, [loadMaintenance, loadChecks])

  const addMaintenance = async () => {
    if (!svc.trim()) return
    await apiJson('/api/settings/maintenance', {
      method: 'POST', body: JSON.stringify({ service: svc.trim(), minutes: mins, reason: '手动设置' }),
    })
    await loadMaintenance()
    setSvc('')
  }

  const rows: Array<{ label: string; status?: CheckStatus; hint?: string; detail?: string }> = [
    { label: 'PostgreSQL', status: checks?.database?.status, hint: '主数据库 + pgvector', detail: checks?.database?.detail },
    {
      label: 'Prometheus',
      status: checks?.prometheus?.status,
      hint: checks?.prometheus?.url || '未配置',
      detail: checks?.prometheus?.detail,
    },
    {
      label: 'Elasticsearch',
      status: checks?.elasticsearch?.status,
      hint: checks?.elasticsearch?.host || '未配置',
      detail: checks?.elasticsearch?.detail,
    },
    { label: 'LLM', status: checks?.llm?.status, hint: checks?.llm?.model, detail: checks?.llm?.base_url },
    { label: 'Redis', status: checks?.redis?.status, hint: '缓存 / 维护窗口' },
    {
      label: 'GitLab CI',
      status: checks?.gitlab?.status,
      hint: checks?.gitlab?.url || '未配置',
      detail: checks?.gitlab?.detail,
    },
    {
      label: 'ArgoCD',
      status: checks?.argocd?.status,
      hint: checks?.argocd?.environments?.map(e => `${e.env}:${e.status}`).join(' · ') || '未配置',
      detail: checks?.argocd?.environments?.find(e => e.detail)?.detail,
    },
  ]

  return (
    <div className="h-full flex flex-col">
      <header className="shrink-0 glass border-b border-shark-border flex items-center px-6 h-14">
        <h1 className="text-sm font-semibold text-white flex items-center gap-2">
          <Settings size={16} /> 平台设置
        </h1>
        <span className="ml-3 text-xs text-shark-muted">环境: {checks?.environment || '…'}</span>
        <button
          onClick={() => loadChecks(true)}
          disabled={checksLoading}
          className="ml-auto text-xs text-shark-muted hover:text-white flex items-center gap-1 disabled:opacity-50"
        >
          {checksLoading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          重新检测
        </button>
        <Link href="/security" className="ml-3 text-xs text-shark-muted hover:text-white">安全合规 →</Link>
      </header>

      <div className="flex-1 overflow-auto p-6 max-w-3xl space-y-6">
        {error && <ErrorState message={error} onRetry={() => loadChecks(true)} />}

        <section className="glass rounded-xl p-5">
          <h3 className="text-sm font-semibold text-white mb-1">运行环境</h3>
          <p className="text-[10px] text-shark-muted mb-3">切换后 UI 按所选环境过滤；各环境使用下方单独粘贴入库的 kubeconfig，与本地 kubectl 无关。</p>
          {environments.length > 1 ? (
            <select
              value={active}
              onChange={e => switchEnv(e.target.value)}
              className="w-full max-w-xs bg-shark-bg border border-shark-border rounded-lg px-3 py-2 text-sm text-white"
            >
              {environments.map(env => (
                <option key={env.id} value={env.id}>{env.label} ({env.clusterName})</option>
              ))}
            </select>
          ) : (
            <p className="text-xs text-shark-muted">
              当前仅识别到 <span className="text-white">{active || checks?.environment || 'test'}</span>。
              若需 dev/test 切换，请确认 <code className="text-shark-accent">backend/config/environments.json</code> 已配置并执行 <code className="text-shark-accent">docker compose up -d --build api frontend</code>。
            </p>
          )}
        </section>

        <section className="glass rounded-xl p-5">
          <h3 className="text-sm font-semibold text-white mb-1">K8s 集群凭证</h3>
          <p className="text-[10px] text-shark-muted mb-4">
            将各环境 kubeconfig 粘贴保存到数据库，部署到服务器时无需拷贝本地文件。保存时会自动测试连通性。
          </p>
          <KubeconfigEditor environments={environments} />
        </section>

        <section className="glass rounded-xl p-5">
          <h3 className="text-sm font-semibold text-white mb-1">集成健康检查</h3>
          <p className="text-[10px] text-shark-muted mb-4">并行探测，结果缓存 45 秒。Prometheus 内网自签证书请设 PROMETHEUS_SSL_VERIFY=false</p>
          <div className="space-y-3">
            {rows.map(row => (
              <div key={row.label} className="flex items-start gap-3 py-2 border-b border-shark-border last:border-0">
                {checksLoading && !checks ? (
                  <Loader2 size={16} className="animate-spin text-shark-muted mt-0.5" />
                ) : (
                  <StatusIcon status={row.status as CheckStatus} />
                )}
                <span className="text-sm text-white w-28 shrink-0">{row.label}</span>
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-shark-muted truncate">{row.hint || '—'}</div>
                  {row.detail && (
                    <div className="text-[10px] text-amber-400/90 mt-0.5 break-words">{row.detail}</div>
                  )}
                </div>
              </div>
            ))}
          </div>
          {checks?.alertmanager && (
            <p className="mt-4 text-xs text-shark-muted">
              Alertmanager Webhook: <code className="text-purple-300">{checks.alertmanager.webhook}</code>
              {checks.alertmanager.token_required ? ' · 需要 X-Alert-Token' : ''}
            </p>
          )}
        </section>

        <section className="glass rounded-xl p-5">
          <h3 className="text-sm font-semibold text-white mb-3">维护窗口（抑制告警）</h3>
          <div className="flex gap-2 mb-3">
            <input value={svc} onChange={e => setSvc(e.target.value)} placeholder="服务名"
              className="flex-1 bg-slate-950/70 border border-shark-border rounded px-2 py-1.5 text-xs text-white" />
            <input type="number" value={mins} onChange={e => setMins(Number(e.target.value))}
              className="w-20 bg-slate-950/70 border border-shark-border rounded px-2 py-1.5 text-xs text-white" />
            <button onClick={addMaintenance} className="text-xs px-3 py-1.5 rounded bg-shark-accent text-white">添加</button>
          </div>
          {maintenance.length === 0 ? (
            <p className="text-xs text-shark-muted">无活跃维护窗口。</p>
          ) : maintenance.map(w => (
            <div key={w.service} className="text-xs text-shark-muted py-1">{w.service} · {w.reason} · 至 {new Date(w.until * 1000).toLocaleString()}</div>
          ))}
        </section>

        <section className="glass rounded-xl p-5 border border-shark-accent/10">
          <h3 className="text-sm font-semibold text-white mb-2">配置说明</h3>
          <p className="text-xs text-shark-muted leading-relaxed">
            普通项 <code className="text-shark-accent">backend/.env</code>，密钥 <code className="text-shark-accent">backend/.env.secrets</code>。
            Prometheus 示例：<code className="text-shark-accent">PROMETHEUS_URL=https://prometheus.example.com</code>，
            nginx Basic Auth 配 <code className="text-shark-accent">PROMETHEUS_USERNAME</code> +
            <code className="text-shark-accent"> PROMETHEUS_PASSWORD</code>（放 .env.secrets）。
            自签证书加 <code className="text-shark-accent">PROMETHEUS_SSL_VERIFY=false</code>。
            LLM 使用 OpenAI 兼容 API：<code className="text-shark-accent">LLM_API_KEY</code>、
            <code className="text-shark-accent">LLM_BASE_URL</code>、
            <code className="text-shark-accent">LLM_MODEL</code>（Key 放 .env.secrets）。
          </p>
        </section>
      </div>
    </div>
  )
}
