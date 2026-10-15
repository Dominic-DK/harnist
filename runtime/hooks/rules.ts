// 순수 함수만 — 설정 해석과 주입 깎기. register.ts 가 이벤트에 붙인다.

export type RoleRule = {
  /** 서브에이전트 모델 (Claude 가 직접 고르면 그대로 둔다) */
  model?: string
  /** 스킬 목록에 남길 이름. 끝의 * 는 접두어. [] 면 목록을 통째로 뺀다. 없으면 그대로 */
  skills?: string[]
  /** 남길 MCP 서버. 안내 절과 지연 도구 목록을 거르고, 나머지 서버 도구는 거부한다 */
  mcp?: string[]
  /** CLAUDE.md 주입. false 면 뺀다, 경로 조각 목록이면 그 파일만 남긴다 */
  memory?: boolean | string[]
  /** 이 역할이 못 쓰는 도구 (끝의 * 는 접두어) */
  denyTools?: string[]
}

export type Config = {
  spawn?: { maxConcurrent?: number; deny?: string[] }
  /** 키는 서브에이전트 종류, '*' 는 규칙이 없는 종류의 기본값 */
  roles?: Record<string, RoleRule>
}

export const match = (pats: string[], name: string) =>
  pats.some(p => (p.endsWith('*') ? name.startsWith(p.slice(0, -1)) : name === p))

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '')

export const serverKept = (keep: string[], server: string) => keep.some(k => norm(server).includes(norm(k)))

export function parseConfig(text: string): Config {
  const raw = JSON.parse(text) as Config
  if (!raw || typeof raw !== 'object') throw new Error('설정이 객체가 아니다')
  return { spawn: raw.spawn ?? {}, roles: raw.roles ?? {} }
}

export const roleFor = (cfg: Config, type: string): RoleRule | undefined => cfg.roles?.[type] ?? cfg.roles?.['*']

/** 생성 판정: 거부 사유 또는 바꿀 모델 */
export function spawnDecision(cfg: Config, type: string, given: string | undefined, running: number) {
  if (match(cfg.spawn?.deny ?? [], type)) return { deny: `harnist runtime: ${type} 서브에이전트는 이 레포에서 금지돼 있다` }
  const max = cfg.spawn?.maxConcurrent
  if (max && running >= max) return { deny: `harnist runtime: 동시 서브에이전트 상한 ${max}개에 닿았다. 끝난 뒤 다시 띄워라` }
  const model = given ? undefined : roleFor(cfg, type)?.model
  return { model }
}

/** 스킬 목록: 머리말은 두고 `- 이름` 항목만 거른다 (이어지는 줄은 앞 항목에 붙는다) */
export function trimSkills(text: string, keep: string[]): string | null {
  const lines = text.split('\n')
  const head: string[] = []
  const items: string[][] = []
  for (const l of lines) {
    if (l.startsWith('- ')) items.push([l])
    else if (items.length) items[items.length - 1]!.push(l)
    else head.push(l)
  }
  const kept = items.filter(it => match(keep, it[0]!.slice(2).split(': ')[0]!.trim()))
  if (!kept.length) return null
  return [...head, ...kept.flat()].join('\n')
}

/** MCP 서버 안내: `## 서버` 절 단위로 거른다 */
export function trimMcp(text: string, keep: string[]): string | null {
  const parts = text.split(/^(?=## )/m)
  const head = parts[0]!.startsWith('## ') ? '' : parts.shift()!
  const kept = parts.filter(p => serverKept(keep, p.slice(3).split('\n')[0]!.trim()))
  return kept.length ? head + kept.join('') : null
}

/** 지연 도구 목록: 남기지 않는 서버의 mcp__ 줄을 뺀다 */
export function trimDeferred(text: string, keep: string[]): string {
  return text
    .split('\n')
    .filter(l => {
      const m = /^mcp__(.+?)__/.exec(l.trim())
      return !m || serverKept(keep, m[1]!)
    })
    .join('\n')
}

/** CLAUDE.md 주입: `Contents of <경로>` 블록 단위로 거른다 */
export function trimMemory(text: string, rule: boolean | string[]): string | null {
  if (rule === true) return text
  if (rule === false) return null
  const parts = text.split(/^(?=Contents of )/m)
  const head = parts[0]!.startsWith('Contents of ') ? '' : parts.shift()!
  const kept = parts.filter(p => rule.some(r => p.split('\n')[0]!.includes(r)))
  return kept.length ? head + kept.join('') : null
}

/** 주입 하나를 역할에 맞게. undefined 면 손대지 않는다 */
export function trimAttachment(type: string, text: string, role: RoleRule): string | null | undefined {
  if (type === 'skill_listing' && role.skills) return trimSkills(text, role.skills)
  if (type === 'mcp_instructions_delta' && role.mcp) return trimMcp(text, role.mcp)
  if (type === 'deferred_tools_delta' && role.mcp) return trimDeferred(text, role.mcp)
  if (type === 'instructions' && role.memory !== undefined) return trimMemory(text, role.memory)
  return undefined
}

/** 도구 거부 사유 */
export function toolBan(role: RoleRule, tool: string): string | undefined {
  if (match(role.denyTools ?? [], tool)) return `harnist runtime: 이 역할은 ${tool} 을 쓸 수 없다`
  const m = /^mcp__(.+?)__/.exec(tool)
  if (m && role.mcp && !serverKept(role.mcp, m[1]!)) return `harnist runtime: 이 역할에는 ${m[1]} MCP 가 열려 있지 않다`
  return undefined
}
