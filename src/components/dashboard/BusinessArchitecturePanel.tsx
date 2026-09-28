'use client'

import Link from 'next/link'
import { ArrowRight, Box, GitBranch, Layers, Server } from 'lucide-react'
import { cn, healthDot, fmtTime, severityBadge } from '@/lib/utils'

export interface ArchComponent {
  name: string
  label: string
  namespace?: string
  type?: string
  health?: string
  found?: boolean
  replicas?: number
  readyReplicas?: number
  host?: string
  matchedName?: string
}

export interface ArchLayer {
  id: string
  name: string
  summary: string
  components: ArchComponent[]
}

export interface HomeOverview {
  environment: string
  clusterName: string
  discoveryPending?: boolean
  stats: {
    services: number
    middlewares: number
    clusters: number
    nodes: number
    healthy: number
    degraded: number
    critical: number
    blueprintTotal?: number
    blueprintFound?: number
    blueprintMissing?: number
    activeIncidents: number
  }
  architecture: {
    title: string
    tagline: string
    layers: ArchLayer[]
    flows: { name: string; steps: string[] }[]
    infra: { name: string; desc: string }[]
    namespaces: { name: string; desc: string }[]
  }
  incidents: Array<{
    id: string
    title: string
    severity: string
    status: string
    createdAt: string
  }>
}

interface Props {
  data: HomeOverview
}

function healthBorder(h?: string) {
  if (h === 'critical') return 'border-red-400/40 bg-red-400/5'
  if (h === 'degraded') return 'border-amber-400/40 bg-amber-400/5'
  if (h === 'healthy') return 'border-emerald-400/30 bg-emerald-400/5'
  return 'border-shark-border bg-slate-900/30'
}

export default function BusinessArchitecturePanel({ data }: Props) {
  const { architecture: arch, stats } = data

  return (
    <div className="space-y-5">
      <div className="glass rounded-xl border border-shark-border p-5">
        <div className="flex flex-wrap items-start gap-4">
          <div className="flex-1 min-w-[200px]">
            <h2 className="text-lg font-bold text-white flex items-center gap-2">
              <Layers size={18} className="text-shark-accent" />
              {arch.title || '业务架构'}
            </h2>
            <p className="text-sm text-shark-muted mt-2 leading-relaxed max-w-3xl">{arch.tagline}</p>
          </div>
          <div className="flex flex-wrap gap-2 text-[10px]">
            {arch.namespaces.map(ns => (
              <span key={ns.name} className="px-2.5 py-1 rounded-full border border-cyan-500/30 bg-cyan-500/10 text-cyan-200">
                {ns.name} · {ns.desc}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-5">
        <div className="xl:col-span-2 space-y-4">
          {arch.layers.map(layer => (
            <div key={layer.id} className="glass rounded-xl border border-shark-border overflow-hidden">
              <div className="px-4 py-3 border-b border-shark-border/60 bg-slate-900/40">
                <div className="text-sm font-semibold text-white">{layer.name}</div>
                <div className="text-[11px] text-shark-muted mt-0.5">{layer.summary}</div>
              </div>
              <div className="p-4 flex flex-wrap gap-2">
                {layer.components.map(c => (
                  <div key={c.name}
                    className={cn('rounded-lg border px-3 py-2 min-w-[140px] max-w-[200px]', healthBorder(c.health))}>
                    <div className="flex items-center gap-1.5">
                      <span className={cn('w-1.5 h-1.5 rounded-full shrink-0', healthDot[c.health || 'unknown'])} />
                      <span className="text-xs font-medium text-white truncate">{c.label || c.name}</span>
                    </div>
                    <div className="text-[10px] text-shark-muted mt-1 font-mono truncate">{c.name}</div>
                    {c.found && c.replicas != null ? (
                      <div className="text-[10px] text-shark-muted mt-0.5">{c.readyReplicas}/{c.replicas} ready</div>
                    ) : c.found && c.host ? (
                      <div className="text-[10px] text-shark-muted mt-0.5 truncate" title={c.host}>{c.host}</div>
                    ) : (
                      <div className="text-[10px] text-shark-muted/60 mt-0.5">{c.found ? '已发现' : '未同步'}</div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>

        <div className="space-y-4">
          <div className="glass rounded-xl border border-shark-border p-4">
            <h3 className="text-xs font-semibold text-shark-muted uppercase mb-3 flex items-center gap-1">
              <GitBranch size={12} /> 关键链路
            </h3>
            <div className="space-y-3">
              {arch.flows.map(f => (
                <div key={f.name} className="text-xs">
                  <div className="text-white font-medium mb-1.5">{f.name}</div>
                  <div className="flex flex-wrap items-center gap-1 text-[10px] text-shark-muted">
                    {f.steps.map((step, i) => (
                      <span key={i} className="flex items-center gap-1">
                        {i > 0 && <ArrowRight size={10} className="text-shark-accent/60 shrink-0" />}
                        <span className="px-1.5 py-0.5 rounded bg-slate-800/80 border border-shark-border">{step}</span>
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="glass rounded-xl border border-shark-border p-4">
            <h3 className="text-xs font-semibold text-shark-muted uppercase mb-3 flex items-center gap-1">
              <Box size={12} /> 基础设施
            </h3>
            <ul className="space-y-2">
              {arch.infra.map(item => (
                <li key={item.name} className="text-[11px]">
                  <span className="text-white font-medium">{item.name}</span>
                  <span className="text-shark-muted"> — {item.desc}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="glass rounded-xl border border-shark-border p-4 text-[11px] text-shark-muted">
            <div className="flex items-center gap-2 text-white font-medium mb-2">
              <Server size={14} /> 实时状态
            </div>
            <p>已发现 {stats.services} 个服务、{stats.middlewares} 个中间件</p>
            <p className="mt-1">蓝图组件 {stats.blueprintFound ?? 0}/{stats.blueprintTotal ?? 0} 已同步
              {(stats.blueprintMissing ?? 0) > 0 && (
                <span className="text-amber-400"> · {stats.blueprintMissing} 未同步</span>
              )}
            </p>
            <p className="mt-1">
              健康 {stats.healthy} · 降级 {stats.degraded} · 异常 {stats.critical}
            </p>
            <Link href="/topology" className="inline-block mt-2 text-shark-accent hover:underline">
              要看调用拓扑去拓扑页 →
            </Link>
          </div>
        </div>
      </div>
    </div>
  )
}

export function IncidentsSidebar({ incidents }: { incidents: HomeOverview['incidents'] }) {
  const active = incidents.filter(i => i.status !== 'resolved')
  return (
    <div className="w-[280px] shrink-0 border-l border-shark-border overflow-auto p-4">
      <h3 className="text-sm font-semibold text-white mb-3">活跃事件 ({active.length})</h3>
      {active.length === 0 && <p className="text-xs text-shark-muted">无活跃告警</p>}
      <div className="space-y-2">
        {active.slice(0, 8).map(inc => (
          <Link key={inc.id} href="/incidents"
            className={cn('block rounded-lg p-3 border transition-colors hover:border-shark-accent/30',
              inc.severity === 'critical' ? 'border-red-400/20 bg-red-400/5' : 'border-shark-border bg-shark-card/30')}>
            <div className="flex items-center gap-1 mb-1">
              <span className={cn('px-1.5 py-0.5 rounded text-[10px] font-medium border', severityBadge[inc.severity])}>
                {inc.severity === 'critical' ? '紧急' : '警告'}
              </span>
              <span className="text-[10px] text-shark-muted">{fmtTime(inc.createdAt)}</span>
            </div>
            <p className="text-xs font-medium text-white line-clamp-2">{inc.title}</p>
          </Link>
        ))}
      </div>
    </div>
  )
}
