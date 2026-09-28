'use client'

import { useCallback, useEffect, useState, type ComponentType } from 'react'
import Link from 'next/link'
import { AlertTriangle, DollarSign, Layers, Rocket, Server } from 'lucide-react'
import { apiJson } from '@/lib/api'
import { ErrorState, LoadingSpinner } from '@/components/ui/AsyncState'
import { useEnvironments } from '@/lib/useEnvironments'
import BusinessArchitecturePanel, {
  IncidentsSidebar,
  type HomeOverview,
} from '@/components/dashboard/BusinessArchitecturePanel'

export default function DashboardPage() {
  const { active: activeEnv } = useEnvironments()
  const [data, setData] = useState<HomeOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await apiJson<HomeOverview>('/api/home/overview'))
    } catch (e) {
      setError(e instanceof Error ? e.message : '无法连接 API')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchData() }, [fetchData, activeEnv])

  if (loading) return <LoadingSpinner />
  if (error || !data) return <ErrorState message={error || '无数据'} onRetry={fetchData} />

  const { stats } = data

  return (
    <div className="h-full flex flex-col">
      <header className="shrink-0 glass border-b border-shark-border flex items-center px-6 h-14 gap-3">
        <Layers size={16} className="text-shark-accent" />
        <h1 className="text-sm font-semibold text-white">全局总览</h1>
        <span className="text-[10px] text-shark-muted bg-shark-accent/10 border border-shark-accent/20 px-2 py-0.5 rounded">
          {data.environment} · {data.clusterName}
        </span>
        {data.discoveryPending && (
          <span className="text-[10px] text-amber-400">正在发现 {data.clusterName} 集群…</span>
        )}
        <button onClick={fetchData} className="ml-auto text-xs text-shark-muted hover:text-white">刷新</button>
      </header>

      <div className="flex-1 flex overflow-hidden">
        <div className="flex-1 overflow-auto p-6">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
            <KpiCard icon={Server} label="业务服务" value={stats.services}
              sub={`健康 ${stats.healthy}`} href="/pods" />
            <KpiCard icon={AlertTriangle} label="活跃事件" value={stats.activeIncidents}
              sub="个" accent={stats.activeIncidents > 0} href="/incidents" />
            <KpiCard icon={Rocket} label="发布" value="CI" sub="GitLab + ArgoCD" href="/deployments" />
            <KpiCard icon={DollarSign} label="节点" value={stats.nodes}
              sub={`${stats.clusters} 集群`} href="/monitoring" />
          </div>

          <BusinessArchitecturePanel data={data} />

          <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-3">
            <QuickLink href="/deployments" label="发布管理" desc="构建状态 · 回滚" />
            <QuickLink href="/logs" label="日志监控" desc="关键字告警" />
            <QuickLink href="/monitoring" label="监控巡检" desc="集群清单 · 报告" />
            <QuickLink href="/incidents" label="事件中心" desc="告警 · 诊断" />
            <QuickLink href="/copilot" label="运维助手" desc="对话查环境" />
          </div>
        </div>

        <IncidentsSidebar incidents={data.incidents} />
      </div>
    </div>
  )
}

function KpiCard({ icon: Icon, label, value, sub, accent, href }: {
  icon: ComponentType<{ size?: number | string; className?: string }>
  label: string; value: string | number; sub: string; accent?: boolean; href?: string
}) {
  const inner = (
    <div className={`glass rounded-xl p-4 ${accent ? 'border-l-2 border-l-red-400' : ''} ${href ? 'hover:border-shark-accent/30 transition-colors cursor-pointer' : ''}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-shark-muted">{label}</span>
        <Icon size={16} className={accent ? 'text-red-400' : 'text-shark-muted'} />
      </div>
      <div className="flex items-baseline gap-1">
        <span className="text-2xl font-bold text-white">{value}</span>
        <span className="text-xs text-shark-muted">{sub}</span>
      </div>
    </div>
  )
  return href ? <Link href={href}>{inner}</Link> : inner
}

function QuickLink({ href, label, desc }: { href: string; label: string; desc: string }) {
  return (
    <Link href={href} className="glass rounded-lg p-3 border border-shark-border hover:border-shark-accent/30 transition-colors">
      <div className="text-sm font-medium text-white">{label}</div>
      <div className="text-[10px] text-shark-muted mt-0.5">{desc}</div>
    </Link>
  )
}
