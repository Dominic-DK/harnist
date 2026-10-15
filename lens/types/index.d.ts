export type Category = { name: string; tokens: number; color: string; kind: string }
// tokens = 컨텍스트에 실제로 실린 토큰, deferred = 도구 검색 뒤에 대기 중인 토큰(이름만 실림)
export type LoadedItem = { kind: 'skill' | 'memory' | 'mcp' | 'agent'; name: string; source: string; tokens: number; deferred: number }
export type Snapshot = {
  at: number
  total: number
  window: number
  categories: Category[]
  items: LoadedItem[]
  // 자동 컴팩션이 도는 토큰 수, 꺼져 있으면 0
  autoCompact: number
}
export type Step = {
  turnId: string
  index: number
  model: string
  agentId: string
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  at: number
  // 히트율이 절반 아래로 떨어진 이유: cold | model | gap | other, 아니면 빈 문자열
  brk: string
  // 요청 시작부터 끝까지, 첫 청크까지(ms)
  ms: number
  ttft: number
}
export type ToolStat = { tool: string; count: number; ms: number; failed: number }
export type TurnSummary = {
  turnId: string
  durationMs: number
  steps: number
  tools: number
  hookChars: number
  hit: number
  at: number
  // 턴이 끝났을 때 다음 요청이 다시 보낼 컨텍스트 토큰
  ctx: number
  // 0~100, 감점 사유 키(aborted·error·failed·denied·asked·repeat·compacted)
  score: number
  reasons: string[]
}
export type Limit = { kind: string; percentUsed: number; resetsAt: string }
export type Limits = { rateLimits: Limit[]; costUsd: number; at: number }
export type Live = { turnStartedAt: number; tool: string; toolStartedAt: number; agents: number }
export type Checks = { asked: number; denied: number }

declare module 'claude-code' {
  interface PluginState {
    lens: {
      snapshot: Snapshot | null
      steps: Step[]
      turns: TurnSummary[]
      toolStats: ToolStat[]
      used: string[]
      startHookChars: number
      band: boolean
      lang: string
      tab: string
      limits: Limits | null
      live: Live | null
      checks: Checks
      ttl: string
      tick: number
    }
  }
}
