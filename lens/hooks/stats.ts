import type { SessionContextBreakdown } from 'claude-code'

import type { LoadedItem, Snapshot, Step, ToolStat, TurnSummary } from '../types'

// 순수 함수만 둔다 — 패널, report 도구, 테스트가 함께 쓴다.

export const hitRate = (s: Pick<Step, 'input' | 'cacheRead' | 'cacheWrite'>) => {
  const all = s.input + s.cacheRead + s.cacheWrite
  return all === 0 ? 0 : s.cacheRead / all
}

const SPARK = '▁▂▃▄▅▆▇█'
export const spark = (rates: number[]) => rates.map(r => SPARK[Math.min(7, Math.floor(r * 8))]).join('')

// 0~1 비율을 칸 수만큼의 가로 막대로 (1/8 칸 단위)
const EIGHTHS = ['', '▏', '▎', '▍', '▌', '▋', '▊', '▉']
export const bar = (ratio: number, cells: number) => {
  const units = Math.round(Math.max(0, Math.min(1, ratio)) * cells * 8)
  return '█'.repeat(Math.floor(units / 8)) + EIGHTHS[units % 8]
}

export const k = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k` : String(n))

export const mcpServer = (tool: string) => tool.match(/^mcp__(.+?)__/)?.[1]
export const norm = (s: string) => s.replace(/[^A-Za-z0-9]+/g, '_').toLowerCase()

export const usedKey = (item: Pick<LoadedItem, 'kind' | 'name'>) =>
  item.kind === 'skill' ? `skill:${item.name}` : item.kind === 'mcp' ? `mcp:${norm(item.name)}` : item.kind === 'agent' ? `agent:${item.name}` : ''

export const itemsOf = (b: SessionContextBreakdown): LoadedItem[] => {
  const servers = new Map<string, { tokens: number; deferred: number }>()
  for (const t of b.mcpTools) {
    const row = servers.get(t.serverName) ?? { tokens: 0, deferred: 0 }
    if (t.isLoaded) row.tokens += t.tokens
    else row.deferred += t.tokens
    servers.set(t.serverName, row)
  }
  return [
    ...(b.skills?.skillFrontmatter ?? []).map(s => ({
      kind: 'skill' as const,
      name: s.name,
      source: s.pluginName ? `plugin:${s.pluginName}` : s.source,
      tokens: s.tokens,
      deferred: 0,
    })),
    ...b.memoryFiles.map(m => ({ kind: 'memory' as const, name: m.path, source: m.type, tokens: m.tokens, deferred: 0 })),
    ...[...servers].map(([name, t]) => ({ kind: 'mcp' as const, name, source: 'mcp', ...t })),
    ...b.agents.map(a => ({ kind: 'agent' as const, name: a.agentType, source: a.source, tokens: a.tokens, deferred: 0 })),
  ]
}

export const fit = (items: LoadedItem[], usedKeys: string[]) => {
  const seen = new Set(usedKeys)
  const out: Record<string, { loaded: number; used: number }> = {}
  for (const it of items) {
    if (it.kind === 'memory') continue
    const row = (out[it.kind] ??= { loaded: 0, used: 0 })
    row.loaded += 1
    if (seen.has(usedKey(it))) row.used += 1
  }
  return out
}

// 히트율이 절반 아래로 떨어진 요청의 원인을 추정한다
export type BreakCause = 'cold' | 'model' | 'gap' | 'other'
export const breakCause = (prev: Step | undefined, cur: Pick<Step, 'model' | 'at'>, rate: number): BreakCause | '' => {
  if (rate >= 0.5) return ''
  if (!prev) return 'cold'
  if (prev.model !== cur.model) return 'model'
  if (cur.at - prev.at > 5 * 60 * 1000) return 'gap'
  return 'other'
}

// ---- 세션 기록(jsonl 행) 요약 — report 도구가 쓴다 ----

type Row = Record<string, unknown> & { kind?: string; v?: number }
const num = (v: unknown) => (typeof v === 'number' ? v : 0)
const str = (v: unknown) => (typeof v === 'string' ? v : '')

export type SessionDigest = {
  id: string
  cwd: string
  model: string
  turns: number
  requests: number
  cacheHit: number | null
  breaks: Record<string, number>
  tools: Record<string, { count: number; ms: number }>
  skills: string[]
  agents: string[]
  baseLoad: number | null
  categories: { name: string; tokens: number }[]
  hookStartChars: number
  hookCharsPerTurn: number | null
  avgTurnSec: number | null
  idle: { id: string; source: string; tokens: number }[]
}

export const digest = (id: string, rows: Row[]): SessionDigest | null => {
  const d: SessionDigest = {
    id,
    cwd: '',
    model: '',
    turns: 0,
    requests: 0,
    cacheHit: null,
    breaks: {},
    tools: {},
    skills: [],
    agents: [],
    baseLoad: null,
    categories: [],
    hookStartChars: 0,
    hookCharsPerTurn: null,
    avgTurnSec: null,
    idle: [],
  }
  let read = 0
  let inputs = 0
  let hookTurns = 0
  let turnMs = 0
  let items: LoadedItem[] = []
  const used = new Set<string>()
  let any = false
  for (const r of rows) {
    if (r.v !== 1) continue
    any = true
    if (r.kind === 'session') {
      d.cwd = str(r.cwd)
      d.model = str(r.model)
    } else if (r.kind === 'step' && !r.agentId) {
      d.requests += 1
      read += num(r.cacheRead)
      inputs += num(r.cacheRead) + num(r.cacheWrite) + num(r.input)
      const brk = str(r.brk)
      if (brk) d.breaks[brk] = (d.breaks[brk] ?? 0) + 1
    } else if (r.kind === 'tool') {
      const name = str(r.tool)
      const row = (d.tools[name] ??= { count: 0, ms: 0 })
      row.count += 1
      row.ms += num(r.ms)
      const server = mcpServer(name)
      if (server) used.add(`mcp:${norm(server)}`)
    } else if (r.kind === 'skill') {
      used.add(`skill:${str(r.skill)}`)
      if (!d.skills.includes(str(r.skill))) d.skills.push(str(r.skill))
    } else if (r.kind === 'agent') {
      used.add(`agent:${str(r.type)}`)
      d.agents.push(str(r.type))
    } else if (r.kind === 'turn') {
      d.turns += 1
      turnMs += num(r.durationMs)
    } else if (r.kind === 'attachment' && !r.replay) {
      const origin = (r.origin ?? {}) as { kind?: string; event?: string }
      if (origin.kind === 'hook') {
        if (origin.event === 'SessionStart') d.hookStartChars += num(r.chars)
        else hookTurns += num(r.chars)
      }
    } else if (r.kind === 'snapshot' && (d.baseLoad === null || r.detail === 'full')) {
      const cats = (r.categories ?? []) as { name: string; tokens: number; kind: string }[]
      const live = cats.filter(c => c.kind === 'used' && c.name !== 'Messages')
      d.baseLoad = live.reduce((s, c) => s + c.tokens, 0)
      d.categories = live.map(c => ({ name: c.name, tokens: c.tokens }))
      items = (r.items ?? []) as LoadedItem[]
    }
  }
  if (!any) return null
  d.cacheHit = inputs ? Math.round((read / inputs) * 1000) / 1000 : null
  d.hookCharsPerTurn = d.turns ? Math.round(hookTurns / d.turns) : null
  d.avgTurnSec = d.turns ? Math.round(turnMs / d.turns / 100) / 10 : null
  d.idle = items
    // lens 0.1 기록의 MCP 항목은 대기 중인 스키마까지 합쳐 셌으므로(deferred 값 없음) 실린 토큰에서 뺀다
    .filter(it => !(it.kind === 'mcp' && it.deferred === undefined))
    .filter(it => it.kind !== 'memory' && it.tokens > 0 && !used.has(usedKey(it)))
    .sort((a, b) => b.tokens - a.tokens)
    .slice(0, 8)
    .map(it => ({ id: usedKey(it), source: it.source, tokens: it.tokens }))
  return d
}

// 여러 세션을 합쳐 무엇을 주로 하는지와 늘 실리지만 안 쓰는 것을 본다
export const rollup = (list: SessionDigest[]) => {
  const tools: Record<string, number> = {}
  const skills: Record<string, number> = {}
  const idle: Record<string, { tokens: number; sessions: number; source: string }> = {}
  const breaks: Record<string, number> = {}
  const repos: Record<string, number> = {}
  let hits = 0
  let hitN = 0
  for (const d of list) {
    repos[d.cwd] = (repos[d.cwd] ?? 0) + 1
    for (const [n, v] of Object.entries(d.tools)) tools[n] = (tools[n] ?? 0) + v.count
    for (const s of [...d.skills, ...d.agents]) skills[s] = (skills[s] ?? 0) + 1
    for (const [n, v] of Object.entries(d.breaks)) breaks[n] = (breaks[n] ?? 0) + v
    for (const it of d.idle) {
      const row = (idle[it.id] ??= { tokens: it.tokens, sessions: 0, source: it.source })
      row.sessions += 1
    }
    if (d.cacheHit !== null) {
      hits += d.cacheHit
      hitN += 1
    }
  }
  const top = <T,>(o: Record<string, T>, by: (v: T) => number, n: number) =>
    Object.entries(o)
      .sort((a, b) => by(b[1]) - by(a[1]))
      .slice(0, n)
  return {
    sessions: list.length,
    repos: top(repos, v => v, 10),
    tools: top(tools, v => v, 15),
    skillsAndAgents: top(skills, v => v, 15),
    avgCacheHit: hitN ? Math.round((hits / hitN) * 1000) / 1000 : null,
    breaks,
    alwaysIdle: top(idle, v => v.tokens * v.sessions, 12).map(([id, v]) => ({ id, ...v })),
  }
}

// ---- 리로드 뒤 같은 세션 기록으로 패널 상태를 다시 채운다 ----

export type Rebuilt = {
  steps: Step[]
  turns: TurnSummary[]
  toolStats: ToolStat[]
  used: string[]
  startHookChars: number
  snapshot: Snapshot | null
}

export const rebuild = (rows: Row[]): Rebuilt => {
  const out: Rebuilt = { steps: [], turns: [], toolStats: [], used: [], startHookChars: 0, snapshot: null }
  const tools = new Map<string, ToolStat>()
  const used = new Set<string>()
  for (const r of rows) {
    if (r.v !== 1) continue
    if (r.kind === 'step') {
      out.steps.push({
        turnId: str(r.turnId),
        index: num(r.index),
        model: str(r.model),
        agentId: str(r.agentId),
        input: num(r.input),
        output: num(r.output),
        cacheRead: num(r.cacheRead),
        cacheWrite: num(r.cacheWrite),
        at: num(r.at) || num(r.t),
        brk: str(r.brk),
        ms: num(r.ms),
        ttft: num(r.ttft),
      })
    } else if (r.kind === 'turn') {
      out.turns.push({
        turnId: str(r.turnId),
        durationMs: num(r.durationMs),
        steps: num(r.steps),
        // 0.2 기록은 tools 대신 toolCount, hit 없음
        tools: num(r.tools) || num(r.toolCount),
        hookChars: num(r.hookChars),
        hit: num(r.hit),
        at: num(r.at) || num(r.t),
        ctx: num(r.ctx),
        score: typeof r.score === 'number' ? r.score : 100,
        reasons: Array.isArray(r.reasons) ? (r.reasons as string[]) : [],
      })
    } else if (r.kind === 'tool') {
      const name = str(r.tool)
      const row = tools.get(name) ?? { tool: name, count: 0, ms: 0, failed: 0 }
      tools.set(name, { ...row, count: row.count + 1, ms: row.ms + num(r.ms), failed: row.failed + (r.isError ? 1 : 0) })
      const server = mcpServer(name)
      if (server) used.add(`mcp:${norm(server)}`)
    } else if (r.kind === 'skill') used.add(`skill:${str(r.skill)}`)
    else if (r.kind === 'agent') used.add(`agent:${str(r.type)}`)
    else if (r.kind === 'attachment' && !r.replay) {
      const origin = (r.origin ?? {}) as { kind?: string; event?: string }
      if (origin.kind === 'hook' && origin.event === 'SessionStart') out.startHookChars += num(r.chars)
    } else if (r.kind === 'snapshot') {
      out.snapshot = {
        at: num(r.at),
        total: num(r.total),
        window: num(r.window),
        categories: (r.categories ?? []) as Snapshot['categories'],
        items: (r.items ?? []) as LoadedItem[],
        autoCompact: num(r.autoCompact),
      }
    }
  }
  out.steps = out.steps.slice(-60)
  out.turns = out.turns.slice(-10)
  out.toolStats = [...tools.values()]
  out.used = [...used]
  return out
}

// ---- 실시간 표시용 계산 ----

export const ttlMs = (ttl: string) => (ttl === '1h' ? 60 * 60 * 1000 : 5 * 60 * 1000)

// 5분을 넘게 쉬었는데도 캐시가 거의 다 맞았으면 TTL 은 1시간이다
export const learnTtl = (prev: Pick<Step, 'at'> | undefined, cur: Pick<Step, 'at'>, rate: number, known: string) => {
  if (known === '1h' || !prev) return known
  const gap = cur.at - prev.at
  return gap > 5 * 60 * 1000 && gap < 60 * 60 * 1000 && rate >= 0.8 ? '1h' : known
}

export const clock = (ms: number) => {
  const sec = Math.max(0, Math.round(ms / 1000))
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const ss = String(sec % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

// 한도 종류 이름을 짧게: five_hour → 5h, seven_day → 7d, 나머지는 그대로
export const limitLabel = (kind: string) =>
  /five|5h|5_hour/i.test(kind) ? '5h' : /seven|week|7d|7_day/i.test(kind) ? '7d' : kind.replace(/_/g, ' ')

// 최근 턴들의 컨텍스트 증가 속도로 자동 컴팩션까지 남은 턴 수를 어림한다
export const compactEta = (turns: Pick<TurnSummary, 'ctx'>[], threshold: number) => {
  const ctxs = turns.map(t => t.ctx).filter(c => c > 0).slice(-6)
  if (!threshold || ctxs.length < 2) return null
  const last = ctxs[ctxs.length - 1] ?? 0
  const growth = (last - (ctxs[0] ?? 0)) / (ctxs.length - 1)
  if (growth <= 0) return null
  return Math.max(0, Math.ceil((threshold - last) / growth))
}

// 생성 속도(첫 토큰 이후 출력 토큰/초)와 첫 토큰까지 평균
export const speed = (list: Pick<Step, 'output' | 'ms' | 'ttft'>[]) => {
  const ok = list.filter(s => s.ms > s.ttft && s.output > 0).slice(-10)
  if (!ok.length) return null
  const out = ok.reduce((a, s) => a + s.output, 0)
  const gen = ok.reduce((a, s) => a + (s.ms - s.ttft), 0)
  return { tokPerSec: Math.round(out / (gen / 1000)), ttftSec: Math.round(ok.reduce((a, s) => a + s.ttft, 0) / ok.length / 100) / 10 }
}

// 창 전체 기준 게이지의 칸 수: 상시 로드 | 대화 | 빈칸(컴팩션 기준선 │ 포함)
export const gauge = (ctx: number, conv: number, window: number, threshold: number, cells: number) => {
  const base = Math.round((Math.max(0, ctx - conv) / window) * cells)
  const convCells = Math.min(cells - base, Math.round((conv / window) * cells))
  const free = Math.max(0, cells - base - convCells)
  const pos = threshold ? Math.round((threshold / window) * cells) - base - convCells : -1
  const mark = pos >= 0 && pos < free ? pos : -1
  return { base, conv: convCells, before: mark >= 0 ? mark : free, mark, after: mark >= 0 ? free - mark - 1 : 0 }
}

// ---- 턴 점수 (본문 없이 잡히는 신호만) ----

export type Signals = {
  aborted: boolean
  error: boolean
  failed: number
  denied: number
  asked: number
  repeat: number
  compacted: number
}

// 100 에서 감점. 중단·오류는 크게, 반복 실패는 따로 한 번 더 깎는다
export const scoreTurn = (s: Signals) => {
  const minus: [keyof Signals, number][] = [
    ['aborted', s.aborted ? 40 : 0],
    ['error', s.error ? 30 : 0],
    ['failed', Math.min(30, s.failed * 10)],
    ['denied', Math.min(20, s.denied * 10)],
    ['asked', Math.min(15, s.asked * 5)],
    ['repeat', s.repeat ? 15 : 0],
    ['compacted', s.compacted ? 10 : 0],
  ]
  const hit = minus.filter(([, n]) => n > 0)
  return { score: Math.max(0, 100 - hit.reduce((a, [, n]) => a + n, 0)), reasons: hit.map(([k]) => k) }
}
