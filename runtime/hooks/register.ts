import type { EngineInterface, Register } from 'claude-code'

import { parseConfig, roleFor, spawnDecision, toolBan, trimAttachment } from './rules'
import type { Config } from './rules'

// 설정: HARNIST_RUNTIME_CONFIG, 레포의 .claude/harnist-runtime.json, ~/.claude/harnist-runtime.json 순.
// 메인 세션은 건드리지 않고, 서브에이전트만 종류(역할)별로 다르게 싣는다.
let cfg: Config | undefined
let cfgPath = ''
let cfgError = ''
const types = new Map<string, string>() // agentId → 서브에이전트 종류
const saved: Record<string, { before: number; after: number; n: number }> = {}
const events = { spawnDenied: 0, modelSet: 0, toolDenied: 0 }

async function load($: EngineInterface) {
  cfg = undefined
  cfgError = ''
  const home = (await $.env.get('HOME')) ?? ''
  // 별도 프로세스 일꾼은 harnist profile 이 HARNIST_RUNTIME_CONFIG 로 자기 설정을 넘긴다
  const given = (await $.env.get('HARNIST_RUNTIME_CONFIG')) ?? ''
  const paths = [given, `${await $.session.cwd()}/.claude/harnist-runtime.json`, `${home}/.claude/harnist-runtime.json`]
  for (const p of paths.filter(Boolean)) {
    if (!(await $.fs.exists(p))) continue
    cfgPath = p
    try {
      cfg = parseConfig(await $.fs.read(p))
    } catch (err) {
      cfgError = String(err)
    }
    return
  }
  cfgPath = ''
}

async function typeOf($: EngineInterface, agentId: string) {
  const known = types.get(agentId)
  if (known) return known
  // 리로드 뒤엔 짝이 비어 있다 — 목록에서 찾는다
  const found = (await $.agent.list()).find(a => a.id === agentId)
  if (found) types.set(agentId, found.type)
  return found?.type
}

function summary() {
  if (!cfgPath) return 'harnist runtime: 설정 없음 (.claude/harnist-runtime.json 또는 ~/.claude/harnist-runtime.json)'
  if (cfgError) return `harnist runtime: ${cfgPath} 을 못 읽었다 — ${cfgError}`
  const roles = Object.entries(cfg?.roles ?? {}).map(([t, r]) => {
    const bits = [
      r.model && `모델 ${r.model}`,
      r.skills && `스킬 ${r.skills.length ? r.skills.join(',') : '없음'}`,
      r.mcp && `MCP ${r.mcp.length ? r.mcp.join(',') : '없음'}`,
      r.memory !== undefined && `CLAUDE.md ${r.memory === false ? '뺌' : r.memory === true ? '그대로' : r.memory.join(',')}`,
      r.denyTools?.length && `금지 ${r.denyTools.join(',')}`,
    ].filter(Boolean)
    return `  ${t.padEnd(18)} ${bits.join(' · ')}`
  })
  const cut = Object.entries(saved).map(
    ([t, s]) => `  ${t.padEnd(24)} ${s.n}회  ${s.before.toLocaleString()} → ${s.after.toLocaleString()}자`,
  )
  const sp = cfg?.spawn ?? {}
  return [
    `harnist runtime — ${cfgPath}`,
    `생성 규칙: 동시 ${sp.maxConcurrent ?? '무제한'}${sp.deny?.length ? ` · 금지 ${sp.deny.join(',')}` : ''}`,
    '역할:',
    ...(roles.length ? roles : ['  (없음)']),
    `이 세션: 모델 지정 ${events.modelSet} · 생성 거부 ${events.spawnDenied} · 도구 거부 ${events.toolDenied}`,
    '깎은 주입:',
    ...(cut.length ? cut : ['  (아직 없음)']),
  ].join('\n')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await load($)
    await $.command.register({
      name: 'runtime',
      description: 'harnist runtime: 역할별 서브에이전트 규칙과 이 세션에서 깎은 양 · reload',
      argumentHint: '[reload]',
      immediate: true,
    })
    return next(e)
  })

  on('command.run', { command: 'runtime' }, async ($, e) => {
    if ((e.args ?? '').trim() === 'reload') await load($)
    return { text: summary() }
  })

  on('agent.spawn', async ($, e, next) => {
    if (!cfg || e.fork) return next(e)
    const running = (await $.agent.list()).filter(a => ['pending', 'running', 'waiting'].includes(a.status)).length
    const d = spawnDecision(cfg, e.subagentType, e.model, running)
    if (d.deny) {
      events.spawnDenied++
      return { deny: d.deny }
    }
    if (d.model) events.modelSet++
    const r = await next(d.model ? { ...e, model: d.model } : e)
    if (r.agentId) types.set(r.agentId, e.subagentType)
    return r
  }).catch(($, e, next) => next(e))

  on('prompt.attachment', async ($, e, next) => {
    const ev = e as { type?: string; agentId?: string; text?: string }
    if (!cfg || !ev.agentId || !ev.type || typeof ev.text !== 'string') return next(e)
    const type = await typeOf($, ev.agentId)
    const role = type ? roleFor(cfg, type) : undefined
    if (!role) return next(e)
    const text = trimAttachment(ev.type, ev.text, role)
    if (text === undefined) return next(e)
    const s = (saved[ev.type] ??= { before: 0, after: 0, n: 0 })
    s.before += ev.text.length
    s.after += text?.length ?? 0
    s.n++
    // 깎은 텍스트는 아래 플러그인(lens 등)도 보게 넘긴다. 통째로 뺄 때만 여기서 끝낸다
    return text === null ? { text: null } : next({ ...e, text })
  })

  on('tool.check', async ($, e, next) => {
    if (!cfg || !e.agentId) return next(e)
    const type = await typeOf($, e.agentId)
    const role = type ? roleFor(cfg, type) : undefined
    const reason = role && toolBan(role, e.tool)
    if (!reason) return next(e)
    events.toolDenied++
    return { decision: 'deny', reason }
  })
}
