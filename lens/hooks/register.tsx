import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Snapshot, Step, ToolStat, TurnSummary } from '../types'
import { pickLang, t } from './i18n'
import type { Key, Lang } from './i18n'
import {
  bar,
  breakCause,
  clock,
  compactEta,
  digest,
  fit,
  gauge,
  hitRate,
  itemsOf,
  k,
  learnTtl,
  limitLabel,
  mcpServer,
  norm,
  assign,
  parseExperiments,
  rebuild,
  rollup,
  sectionOverride,
  scoreTurn,
  spark,
  speed,
  ttlMs,
  usedKey,
} from './stats'
import type { Experiment, SessionDigest } from './stats'

// 세션에 실린 것(로드)과 쓰인 것(호출), 요청별 캐시를 구조와 크기만 기록한다. 본문 텍스트는 남기지 않는다.
const PANE = 'lens'
const snapshot = atom({ plugin: 'lens', key: 'snapshot' } as const, null)
const steps = atom({ plugin: 'lens', key: 'steps' } as const, [])
const turns = atom({ plugin: 'lens', key: 'turns' } as const, [])
const toolStats = atom({ plugin: 'lens', key: 'toolStats' } as const, [])
const used = atom({ plugin: 'lens', key: 'used' } as const, [])
const startHookChars = atom({ plugin: 'lens', key: 'startHookChars' } as const, 0)
const band = atom({ plugin: 'lens', key: 'band' } as const, true)
const lang = atom({ plugin: 'lens', key: 'lang' } as const, 'ko')
const tab = atom({ plugin: 'lens', key: 'tab' } as const, 'overview')
const limits = atom({ plugin: 'lens', key: 'limits' } as const, null)
const live = atom({ plugin: 'lens', key: 'live' } as const, null)
const checks = atom({ plugin: 'lens', key: 'checks' } as const, { asked: 0, denied: 0 })
const ttl = atom({ plugin: 'lens', key: 'ttl' } as const, '5m')
// 1초마다 바뀌는 값 — 카운트다운과 경과 시간을 그리는 쪽이 읽어서 다시 그려진다
const tick = atom({ plugin: 'lens', key: 'tick' } as const, 0)

let lines: string[] = []
let dir = ''
let file = ''
let chain: Promise<unknown> = Promise.resolve()
let fullMeasured = false
let turnTools = 0
let turnHookChars = 0
let turnSteps: Step[] = []
let ticks = 0
// 지금 도는 메인 턴과 그 턴의 점수 신호
let currentTurnId = ''
let turnSignals = { asked: 0, denied: 0, failed: 0, compacted: 0, failedTools: {} as Record<string, number> }
// 리로드·재개 직후 첫 요청은 과거 주입 메시지를 다시 렌더링한다. 그동안 들어오는 것은 재생분으로 표시한다
let replaying = false
// 이 세션이 B(변형)를 받은 실험, 그리고 전체 배정
let expB: Experiment[] = []
let expAssigned: { id: string; variant: 'A' | 'B'; op: string; section: string }[] = []
const typeOf: Record<string, string> = {}

async function loadExperiments($: EngineInterface) {
  const path = `${await lensDir($)}/experiments.json`
  if (!(await $.fs.exists(path))) return
  const sid = await $.session.id()
  const list = parseExperiments(await $.fs.read(path))
  expAssigned = list.map(x => ({ id: x.id, variant: assign(sid, x.id), op: x.op, section: x.section ?? '' }))
  expB = list.filter(x => assign(sid, x.id) === 'B')
  for (const a of expAssigned) await rec($, 'experiment', a)
}

const isMain = (s: Step) => s.agentId === ''

async function lensDir($: EngineInterface) {
  if (!dir) dir = `${(await $.env.get('HOME')) ?? '/tmp'}/.claude/lens`
  return dir
}

async function rec($: EngineInterface, kind: string, data: Record<string, unknown>) {
  try {
    lines.push(JSON.stringify({ v: 1, t: Date.now(), kind, ...data }))
    chain = chain.then(async () => {
      if (!file) {
        file = `${await lensDir($)}/${await $.session.id()}.jsonl`
        // 리로드되면 메모리가 비므로, 같은 세션의 이전 기록을 먼저 읽어 이어 붙인다
        if (await $.fs.exists(file)) lines = [...(await $.fs.read(file)).split('\n').filter(Boolean), ...lines]
      }
      await $.fs.write(file, lines.join('\n') + '\n')
    })
    await chain
  } catch (err) {
    $.ui.log(`lens: ${String(err)}`, { to: 'debug' })
    chain = Promise.resolve()
  }
}

// 모드가 (다시) 로드되면 같은 세션의 기록 파일로 패널 상태를 채운다 — 세션 전체를 보여 주기 위해
async function hydrate($: EngineInterface) {
  file = `${await lensDir($)}/${await $.session.id()}.jsonl`
  if (!(await $.fs.exists(file))) return
  const prior = (await $.fs.read(file)).split('\n').filter(Boolean)
  lines = [...prior, ...lines]
  const rows = prior.map(l => {
    try {
      return JSON.parse(l)
    } catch {
      return {}
    }
  })
  const r = rebuild(rows)
  await update($, steps, () => r.steps)
  await update($, turns, () => r.turns)
  await update($, toolStats, () => r.toolStats)
  await update($, used, () => r.used)
  await update($, startHookChars, () => r.startHookChars)
  if (r.snapshot) {
    await update($, snapshot, () => r.snapshot)
    fullMeasured = true
  }
}

async function markUsed($: EngineInterface, key: string) {
  await update($, used, list => (list.includes(key) ? list : [...list, key]))
}

// 첫 턴은 token-count API 로 정확히, 이후는 마지막 응답 기준 추정으로 가볍게 잰다
async function measure($: EngineInterface) {
  const detail = fullMeasured ? 'summary' : 'full'
  fullMeasured = true
  const u = await $.session.usage({ breakdown: detail })
  const b = u.context.breakdown
  if (!b) return
  const snap: Snapshot = {
    at: Date.now(),
    total: b.totalTokens,
    window: u.context.window,
    // 서브에이전트용 색 이름(`green_FOR_SUBAGENTS_ONLY`)은 기본 색 이름으로 돌린다
    categories: b.categories.map(c => ({ name: c.name, tokens: c.tokens, color: c.color.replace(/_FOR_SUBAGENTS_ONLY$/, ''), kind: c.kind })),
    items: itemsOf(b),
    autoCompact: b.isAutoCompactEnabled ? (b.autoCompactThreshold ?? 0) : 0,
  }
  await update($, snapshot, () => snap)
  await rec($, 'snapshot', { detail, ...snap })
}

async function refreshLimits($: EngineInterface) {
  const u = await $.session.usage()
  await update($, limits, () => ({
    rateLimits: u.rateLimits.map(r => ({ kind: r.kind, percentUsed: r.percentUsed, resetsAt: r.resetsAt ?? '' })),
    costUsd: u.cost?.usd ?? 0,
    at: Date.now(),
  }))
}

// 1초마다: 턴이 돌고 있거나 캐시가 살아 있을 때만 다시 그리게 하고, 3초마다 서브에이전트 수를 센다
async function onTick($: EngineInterface) {
  ticks += 1
  const lv = await read($, live)
  const last = (await read($, steps)).filter(isMain).pop()
  const warm = last !== undefined && Date.now() - last.at < ttlMs(await read($, ttl)) + 2000
  if (lv && ticks % 3 === 0) {
    const running = (await $.agent.list()).filter(a => a.status === 'running').length
    if (running !== lv.agents) await update($, live, v => (v ? { ...v, agents: running } : v))
  }
  if (lv || warm) await update($, tick, n => n + 1)
}

async function openPane($: EngineInterface) {
  await $.ui.open({ id: PANE, title: 'Lens' })
  if (!(await read($, snapshot))) void measure($)
  if (!(await read($, limits))) void refreshLimits($).catch(() => undefined)
}

async function askReview($: EngineInterface) {
  const l = (await read($, lang)) as Lang
  await $.prompt.submit({ text: t(l, 'analyzePrompt'), asUser: true })
}

async function setLang($: EngineInterface, next: Lang) {
  await update($, lang, () => next)
  await $.store.set('lang', next)
}

// report 도구: 이 세션 또는 최근 세션들의 요약을 JSON 으로 돌려준다
async function report($: EngineInterface, scope: string, limit: number) {
  if (scope !== 'recent') {
    const me = digest(file || 'current', lines.map(l => JSON.parse(l)))
    return JSON.stringify({ scope: 'session', session: me, checks: await read($, checks), cacheTtl: await read($, ttl) })
  }
  const d = await lensDir($)
  const entries = (await $.fs.list(d)).filter(x => x.kind === 'file' && x.name.endsWith('.jsonl'))
  entries.sort((a, b) => b.mtimeMs - a.mtimeMs)
  const list: SessionDigest[] = []
  for (const x of entries.slice(0, limit)) {
    try {
      const text = await $.fs.read(`${d}/${x.name}`)
      const rows = text
        .split('\n')
        .filter(Boolean)
        .map(l => {
          try {
            return JSON.parse(l)
          } catch {
            return {}
          }
        })
      const one = digest(x.name.replace(/\.jsonl$/, ''), rows)
      if (one) list.push(one)
    } catch {
      // 4MiB 넘는 기록이나 읽기 실패는 건너뛴다
    }
  }
  return JSON.stringify({ scope: 'recent', summary: rollup(list), sessions: list.slice(0, 10) })
}

const scoreColor = (n: number) => (n >= 85 ? 'success' : n >= 60 ? 'warning' : 'error')
const hitColor = (r: number) => (r >= 0.8 ? 'success' : r >= 0.4 ? 'warning' : 'error')
const pctColor = (p: number) => (p >= 85 ? 'error' : p >= 60 ? 'warning' : 'success')

export const register: Register = on => {
  // ---- 도구와 커맨드 ----

  // 자기 도구 호출은 아래 관찰 훅보다 먼저 받아 바로 답한다
  on('tool.call', { tool: 'mcp__lens__report' }, async ($, e) => {
    const ev = e as { scope?: string; limit?: number }
    return { result: await report($, ev.scope ?? 'session', Math.min(50, Math.max(1, ev.limit ?? 20))) }
  }).catch(($, e, next) => ({ result: JSON.stringify({ error: `lens report failed: ${next.error?.message ?? 'unknown'}` }) }))

  on('session.start', async ($, e, next) => {
    replaying = (await $.session.turns()) > 0
    try {
      await hydrate($)
    } catch (err) {
      $.ui.log(`lens: ${String(err)}`, { to: 'debug' })
    }
    try {
      await loadExperiments($)
    } catch (err) {
      $.ui.log(`lens: experiments ${String(err)}`, { to: 'debug' })
    }
    const savedLang = await $.store.get('lang')
    const envLang = await $.env.get('LANG')
    await update($, lang, () => pickLang(savedLang, envLang))
    if ((await $.store.get('band')) === false) await update($, band, () => false)
    if ((await $.store.get('ttl')) === '1h') await update($, ttl, () => '1h')
    $.clock.every(1000, () => void onTick($).catch(() => undefined))
    await rec($, 'session', {
      resumed: replaying,
      cwd: await $.session.cwd(),
      model: await $.session.model(),
      version: (await $.session.version()).version,
      interactive: e.isInteractive,
    })
    await $.tool.register({
      name: 'report',
      description:
        'lens: token load, cache hits, tool/skill usage and unused loaded items for this session (scope "session") or recent sessions (scope "recent"). Used by the lens-review skill.',
      inputSchema: {
        type: 'object',
        properties: { scope: { type: 'string', enum: ['session', 'recent'] }, limit: { type: 'number' } },
      },
    })
    await $.command.register({
      name: 'lens',
      description: 'Open the lens pane · band (toggle the line under the prompt) · lang ko|en · review (analyze) · exp (A/B experiments)',
      argumentHint: '[band | lang ko|en | review | exp]',
      immediate: true,
    })
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    if (e.reason === 'resume') replaying = true
    await rec($, 'session.end', { reason: e.reason })
    return next(e)
  })

  on('command.run', { command: 'lens' }, async ($, e) => {
    const [arg = '', value = ''] = (e.args ?? '').trim().split(/\s+/)
    const l = (await read($, lang)) as Lang
    if (arg === 'band') {
      const shown = !(await read($, band))
      await update($, band, () => shown)
      await $.store.set('band', shown)
      return { text: t(l, shown ? 'bandOn' : 'bandOff') }
    }
    if (arg === 'lang') {
      const chosen: Lang = value === 'en' || value === 'ko' ? value : l === 'ko' ? 'en' : 'ko'
      await setLang($, chosen)
      return { text: t(chosen, 'langSet') }
    }
    if (arg === 'review') {
      void askReview($)
      return {}
    }
    if (arg === 'exp') {
      if (!expAssigned.length) return { text: `lens: no active experiments (${await lensDir($)}/experiments.json)` }
      return { text: 'lens experiments (this session)\n' + expAssigned.map(a => `  ${a.id.padEnd(24)} ${a.variant}  ${a.op} ${a.section}`).join('\n') }
    }
    if (arg !== '') return { text: t(l, 'usage') }
    await openPane($)
    return {}
  })

  // ---- 관찰 ----

  // 실험: B 를 받은 세션만 섹션을 바꾸거나 빼고, append 는 끝에 섹션을 더한다
  on('prompt.section', async ($, e, next) => {
    const r = await next(e)
    if (!expB.length) return r
    return { text: sectionOverride(expB, e.name, r.text) }
  })

  on('prompt.compose', async ($, e, next) => {
    const base = await next(e)
    const extra = expB.filter(x => x.op === 'append' && x.text).map(x => ({ id: `lens:exp-${x.id}`, text: x.text ?? '', scope: 'session' as const }))
    const r = extra.length ? { sections: [...base.sections, ...extra] } : base
    await rec($, 'compose', { model: e.model, sections: r.sections.map(s => ({ id: s.id, scope: s.scope, chars: s.text.length })) })
    return r
  })

  // 훅이 넣는 텍스트는 매 턴 드는 상시 비용이라 따로 센다
  on('prompt.attachment', async ($, e, next) => {
    const ev = e as { type?: string; text?: string; agentId?: string; origin?: { kind: string; event?: string } }
    // 아래 플러그인이 바꾼 뒤의, 모델이 실제로 읽는 텍스트로 센다 (null 이면 빠진 주입)
    const out = await next(e)
    const chars = out.text?.length ?? 0
    // 서브에이전트에 실린 주입은 agentId 로 가른다 (메인은 비워 둔다)
    const agent = ev.agentId ? { agentId: ev.agentId } : {}
    if (replaying) {
      await rec($, 'attachment', { type: ev.type, origin: ev.origin, chars, replay: true, ...agent })
      return out
    }
    if (ev.origin?.kind === 'hook' && !ev.agentId) {
      if (ev.origin.event === 'SessionStart') await update($, startHookChars, n => n + chars)
      else turnHookChars += chars
    }
    await rec($, 'attachment', { type: ev.type, origin: ev.origin, chars, ...agent })
    return out
  })

  on('skill.prompt', async ($, e, next) => {
    await markUsed($, `skill:${e.skill}`)
    await rec($, 'skill', { skill: e.skill, source: e.source, plugin: e.pluginName, chars: e.text.length })
    return next(e)
  })

  on('agent.spawn', async ($, e, next) => {
    await markUsed($, `agent:${e.subagentType}`)
    const r = await next(e)
    // agentId 로 이 서브에이전트의 주입·요청을 종류와 잇는다. 플러그인이 막았으면 사유를 남긴다
    await rec($, 'agent', {
      type: e.subagentType, model: r.model ?? e.model, background: e.background, fork: e.fork,
      ...(r.agentId ? { agentId: r.agentId } : {}), ...(r.deny ? { deny: r.deny } : {}),
    })
    return r
  }).catch(($, e, next) => next(e))

  // 권한을 물었는지·거부됐는지만 센다. 판정은 그대로 둔다
  on('tool.check', async ($, e, next) => {
    const r = await next(e)
    const decision = (r as { decision?: string }).decision ?? ''
    if (decision === 'ask' || decision === 'deny') {
      if (!(e as { agentId?: string }).agentId) turnSignals[decision === 'ask' ? 'asked' : 'denied'] += 1
      await update($, checks, c => (decision === 'ask' ? { ...c, asked: c.asked + 1 } : { ...c, denied: c.denied + 1 }))
      await rec($, 'check', { tool: e.tool, decision })
    }
    return r
  }).catch(($, e, next) => next(e))

  on('tool.call', async ($, e, next) => {
    const started = Date.now()
    const agentId = (e as { agentId?: string }).agentId ?? ''
    if (!agentId) await update($, live, v => (v ? { ...v, tool: e.tool, toolStartedAt: started } : v))
    const r = await next(e)
    const ms = Date.now() - started
    const isError = (r as { isError?: boolean }).isError === true
    if (!agentId) {
      turnTools += 1
      if (isError) {
        turnSignals.failed += 1
        turnSignals.failedTools[e.tool] = (turnSignals.failedTools[e.tool] ?? 0) + 1
      }
      await update($, live, v => (v ? { ...v, tool: '', toolStartedAt: 0 } : v))
    }
    const server = mcpServer(e.tool)
    if (server) await markUsed($, `mcp:${norm(server)}`)
    await update($, toolStats, list => {
      const found = list.find(x => x.tool === e.tool)
      const row: ToolStat = {
        tool: e.tool,
        count: (found?.count ?? 0) + 1,
        ms: (found?.ms ?? 0) + ms,
        failed: (found?.failed ?? 0) + (isError ? 1 : 0),
      }
      return [...list.filter(x => x.tool !== e.tool), row]
    })
    const id = (e as { tool_use_id?: string }).tool_use_id ?? ''
    await rec($, 'tool', { tool: e.tool, id, start: started, ms, agentId, turnId: agentId ? '' : currentTurnId, isError })
    return r
  }).catch(($, e, next) => next(e))

  on('turn.start', async ($, e, next) => {
    const now = Date.now()
    if (!e.agentId) {
      currentTurnId = e.turnId
      turnSignals = { asked: 0, denied: 0, failed: 0, compacted: 0, failedTools: {} }
      await update($, live, () => ({ turnStartedAt: now, tool: '', toolStartedAt: 0, agents: 0 }))
    }
    if (e.agentId && !typeOf[e.agentId]) typeOf[e.agentId] = (await $.agent.list()).find(a => a.id === e.agentId)?.type ?? ''
    await rec($, 'turnStart', { turnId: e.turnId, agentId: e.agentId ?? '', agentType: e.agentId ? typeOf[e.agentId] : '', at: now })
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    if (!e.agentId) turnSignals.compacted += 1
    await rec($, 'compact', { trigger: e.trigger, agentId: e.agentId ?? '' })
    return next(e)
  }).catch(($, e, next) => next(e))

  // 요청이 끝날 때마다 usage 를 받아 캐시 히트·속도를 실시간으로 갱신한다
  on('turn.step', async function* ($, e, next) {
    const begun = Date.now()
    let ttft = 0
    const stream = next(e)
    let piece = await stream.next()
    while (!piece.done) {
      const chunk = piece.value
      if (!ttft && (chunk.kind === 'text' || chunk.kind === 'thinking' || chunk.kind === 'tool')) ttft = Date.now() - begun
      if (chunk.kind === 'stop') replaying = false
      if (chunk.kind === 'stop' && chunk.usage) {
        const now = Date.now()
        const prev = (await read($, steps)).filter(s => s.agentId === (e.agentId ?? '')).pop()
        const base = {
          input: chunk.usage.input_tokens,
          cacheRead: chunk.usage.cache_read_input_tokens,
          cacheWrite: chunk.usage.cache_creation_input_tokens,
        }
        const rate = hitRate(base)
        const step: Step = {
          turnId: e.turnId,
          index: e.index,
          model: chunk.usage.model,
          agentId: e.agentId ?? '',
          output: chunk.usage.output_tokens,
          at: now,
          ...base,
          brk: breakCause(prev, { model: chunk.usage.model, at: now }, rate),
          ms: now - begun,
          ttft,
        }
        if (isMain(step)) {
          turnSteps.push(step)
          const known = await read($, ttl)
          const learned = learnTtl(prev, step, rate, known)
          if (learned !== known) {
            await update($, ttl, () => learned)
            await $.store.set('ttl', learned)
          }
        }
        await update($, steps, list => [...list, step].slice(-60))
        await rec($, 'step', { ...step, stop: chunk.stopReason })
      }
      yield chunk
      piece = await stream.next()
    }
    return piece.value
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId) {
      const info = (await $.agent.list()).find(a => a.id === e.agentId)
      await rec($, 'agentDone', { agentId: e.agentId, type: info?.type || typeOf[e.agentId] || '', durationMs: e.durationMs, usage: e.usage })
    }
    if (!e.agentId) {
      const sc = scoreTurn({
        aborted: e.isAborted || e.reason === 'aborted',
        error: e.reason === 'error',
        failed: turnSignals.failed,
        denied: turnSignals.denied,
        asked: turnSignals.asked,
        repeat: Object.values(turnSignals.failedTools).filter(n => n >= 2).length,
        compacted: turnSignals.compacted,
      })
      const reads = turnSteps.reduce((s, x) => s + x.cacheRead, 0)
      const all = turnSteps.reduce((s, x) => s + x.cacheRead + x.cacheWrite + x.input, 0)
      const lastStep = turnSteps[turnSteps.length - 1]
      const summary: TurnSummary = {
        turnId: e.turnId,
        durationMs: e.durationMs,
        steps: turnSteps.length,
        tools: turnTools,
        hookChars: turnHookChars,
        hit: all ? reads / all : 0,
        at: Date.now(),
        ctx: lastStep ? lastStep.input + lastStep.cacheRead + lastStep.cacheWrite + lastStep.output : 0,
        score: sc.score,
        reasons: sc.reasons,
      }
      await update($, turns, list => [...list, summary].slice(-10))
      await update($, live, () => null)
      await rec($, 'turn', { ...summary, reason: e.reason, usage: e.usage })
      turnTools = 0
      turnHookChars = 0
      turnSteps = []
      currentTurnId = ''
      void measure($).catch(err => rec($, 'error', { at: 'measure', message: String(err) }))
    }
    return next(e)
  })

  // 한도 사용률이나 비용이 바뀌면 들어온다
  on('session.measure', async ($, e, next) => {
    if (e.cost && e.changed.includes('cost')) await rec($, 'cost', { usd: e.cost.usd })
    await update($, limits, () => ({
      rateLimits: e.rateLimits.map(r => ({ kind: r.kind, percentUsed: r.percentUsed, resetsAt: r.resetsAt ?? '' })),
      costUsd: e.cost?.usd ?? 0,
      at: Date.now(),
    }))
    return next(e)
  })

  // ---- 입력창 아래 한 줄 (힌트 줄 자리, 원래 힌트는 위에 남긴다) ----

  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    const others = await next(e)
    if (!(await read($, band))) return others
    const main = (await read($, steps)).filter(isMain)
    const lastStep = main[main.length - 1]
    if (!lastStep) return others
    await read($, tick)
    const { Box, Text } = $.ui.resolve(e)
    const l = (await read($, lang)) as Lang
    const snap = await read($, snapshot)
    const lim = await read($, limits)
    const cacheTtl = await read($, ttl)
    const now = Date.now()
    const rate = hitRate(lastStep)
    const ctx = lastStep.input + lastStep.cacheRead + lastStep.cacheWrite
    const left = ttlMs(cacheTtl) - (now - lastStep.at)
    const fiveH = lim?.rateLimits.find(r => limitLabel(r.kind) === '5h')
    return (
      <Box flexDirection="column">
        {others}
        <Text wrap="truncate-end">
          <Text color="claude">◆ lens </Text>
          <Text color="subtle">{t(l, 'cache')} </Text>
          <Text bold color={hitColor(rate)}>
            {Math.round(rate * 100)}%
          </Text>
          <Text color="subtle"> {spark(main.slice(-12).map(hitRate))} </Text>
          <Text color={left > 60000 ? 'subtle' : left > 0 ? 'warning' : 'error'}>{left > 0 ? `⏱ ${clock(left)}` : '⏱ —'}</Text>
          <Text color="subtle">
            {'  '}
            {t(l, 'context')} {k(ctx)}
            {snap ? `/${k(snap.window)}` : ''}
          </Text>
          {fiveH && (
            <Text>
              <Text color="subtle">{'  '}5h </Text>
              <Text color={pctColor(fiveH.percentUsed)}>{Math.round(fiveH.percentUsed)}%</Text>
            </Text>
          )}
        </Text>
      </Box>
    )
  })

  // ---- 패널 ----

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    await read($, tick)
    const l = (await read($, lang)) as Lang
    const L = (key: Key) => t(l, key)
    const current = await read($, tab)
    const snap = await read($, snapshot)
    const stepList = await read($, steps)
    const turnList = await read($, turns)
    const stats = await read($, toolStats)
    const usedKeys = await read($, used)
    const hookStart = await read($, startHookChars)
    const lv = await read($, live)
    const chk = await read($, checks)
    const cacheTtl = await read($, ttl)
    const now = Date.now()
    const width = Math.max(28, (e.props.bodyColumns ?? 60) - 2)
    const barCells = Math.max(8, Math.min(width - 32, 30))

    const tabButton = (id: string, key: Key, hotkey: string) => (
      <Button key={`tab-${id}`} plain hotkey={hotkey} dimColor={current !== id} onPress={() => void update($, tab, () => id)}>
        {L(key)}
      </Button>
    )
    const title = (text: string, right?: string) => (
      <Box flexDirection="row" justifyContent="space-between">
        <Text color="subtle" bold>
          {text}
        </Text>
        {right ? <Text color="subtle">{right}</Text> : null}
      </Box>
    )

    const header = (
      <Box flexDirection="row" justifyContent="space-between">
        <Box flexDirection="row" gap={2}>
          <Text bold color="claude">
            ◆ Lens
          </Text>
          {tabButton('overview', 'overview', '1')}
          {tabButton('load', 'load', '2')}
          {tabButton('activity', 'activity', '3')}
        </Box>
        <Box flexDirection="row" gap={2}>
          <Button key="review" plain hotkey="a" onPress={() => void askReview($)}>
            {L('analyze')}
          </Button>
          <Button key="lang" plain hotkey="l" dimColor onPress={() => void setLang($, l === 'ko' ? 'en' : 'ko')}>
            {l === 'ko' ? 'EN' : '한'}
          </Button>
        </Box>
      </Box>
    )

    if (!snap) {
      return (
        <Box flexDirection="column" gap={1}>
          {header}
          <Text color="subtle">{L('waiting')}</Text>
        </Box>
      )
    }

    const main = stepList.filter(isMain)
    const lastStep = main[main.length - 1]

    if (current === 'load') {
      const cats = snap.categories.filter(c => c.kind === 'used' && c.tokens > 0).sort((a, b) => b.tokens - a.tokens)
      const maxCat = cats[0]?.tokens ?? 1
      const f = fit(snap.items, usedKeys)
      const top = [...snap.items].filter(i => i.tokens > 0).sort((a, b) => b.tokens - a.tokens).slice(0, 8)
      const maxItem = top[0]?.tokens ?? 1
      const label: Record<string, Key> = { skill: 'skill', mcp: 'mcp', agent: 'agent' }
      return (
        <Box flexDirection="column" gap={1}>
          {header}
          <Box flexDirection="column">
            {title(L('categories'), `${k(snap.total)} / ${k(snap.window)}`)}
            {cats.map(c => (
              <Box key={c.name} flexDirection="row">
                <Box width={22}>
                  <Text wrap="truncate-end">{c.name}</Text>
                </Box>
                <Text color={c.color}>{bar(c.tokens / maxCat, barCells)}</Text>
                <Text color="subtle"> {k(c.tokens)}</Text>
              </Box>
            ))}
          </Box>
          <Box flexDirection="column">
            {title(L('unused'), `${L('startHooks')} ${k(hookStart)}${L('chars')}`)}
            <Box flexDirection="row" gap={3}>
              {Object.entries(f).map(([kind, v]) => (
                <Text key={kind}>
                  {L(label[kind] ?? 'skill')}{' '}
                  <Text bold color={v.loaded - v.used > 0 ? 'warning' : 'success'}>
                    {v.loaded - v.used}
                  </Text>
                  <Text color="subtle">/{v.loaded}</Text>
                </Text>
              ))}
            </Box>
          </Box>
          <Box flexDirection="column">
            {title(L('bigItems'))}
            {top.map(it => {
              const isUsed = usedKeys.includes(usedKey(it))
              return (
                <Box key={`${it.kind}-${it.name}`} flexDirection="row">
                  <Text color={isUsed ? 'success' : 'subtle'}>{isUsed ? '✓ ' : '· '}</Text>
                  <Box width={20}>
                    <Text wrap="truncate-middle">{it.kind === 'memory' ? it.name.replace(/^.*\/(.+\/.+)$/, '…/$1') : it.name}</Text>
                  </Box>
                  <Text color="suggestion">{bar(it.tokens / maxItem, barCells)}</Text>
                  <Text color="subtle"> {k(it.tokens)}</Text>
                </Box>
              )
            })}
          </Box>
        </Box>
      )
    }

    if (current === 'activity') {
      const sorted = [...stats].sort((a, b) => b.count - a.count).slice(0, 8)
      const maxCount = sorted[0]?.count ?? 1
      const usedNames = usedKeys.filter(x => x.startsWith('skill:') || x.startsWith('agent:')).map(x => x.replace(/^\w+:/, ''))
      const sp = speed(main)
      const failed = stats.reduce((s, x) => s + (x.failed ?? 0), 0)
      return (
        <Box flexDirection="column" gap={1}>
          {header}
          <Box flexDirection="column">
            {title(L('tools'), `${stats.reduce((s, x) => s + x.count, 0)}`)}
            {sorted.length === 0 && <Text color="subtle">{L('noTools')}</Text>}
            {sorted.map(s => (
              <Box key={s.tool} flexDirection="row">
                <Box width={22}>
                  <Text wrap="truncate-middle">{s.tool.replace(/^mcp__/, '')}</Text>
                </Box>
                <Text color="claude">{bar(s.count / maxCount, barCells)}</Text>
                <Text color="subtle">
                  {' '}
                  {s.count} · {(s.ms / s.count / 1000).toFixed(1)}s
                </Text>
                {s.failed > 0 && <Text color="error"> ✕{s.failed}</Text>}
              </Box>
            ))}
          </Box>
          <Box flexDirection="row" gap={4}>
            <Box flexDirection="column">
              {title(L('speed'))}
              {sp ? (
                <Text>
                  <Text bold>{sp.tokPerSec}</Text>
                  <Text color="subtle">
                    {' '}
                    {L('tokPerSec')} · {L('firstToken')} {sp.ttftSec}s
                  </Text>
                </Text>
              ) : (
                <Text color="subtle">—</Text>
              )}
            </Box>
            <Box flexDirection="column">
              {title(L('checks'))}
              <Text>
                <Text color={chk.asked ? 'warning' : 'subtle'}>
                  {L('asked')} {chk.asked}
                </Text>
                <Text color="subtle"> · </Text>
                <Text color={chk.denied ? 'error' : 'subtle'}>
                  {L('denied')} {chk.denied}
                </Text>
                <Text color="subtle"> · </Text>
                <Text color={failed ? 'error' : 'subtle'}>
                  {L('failed')} {failed}
                </Text>
              </Text>
            </Box>
          </Box>
          <Box flexDirection="column">
            {title(L('usedSkills'))}
            <Text wrap="wrap" color={usedNames.length ? undefined : 'subtle'}>
              {usedNames.length ? usedNames.join(' · ') : L('none')}
            </Text>
          </Box>
          <Box flexDirection="column">
            {title(L('turns'))}
            {[...turnList]
              .reverse()
              .slice(0, 6)
              .map(tu => (
                <Box key={tu.turnId} flexDirection="row" justifyContent="space-between">
                  <Text>
                    <Text bold color={scoreColor(tu.score ?? 100)}>
                      {String(tu.score ?? 100).padStart(3)}
                    </Text>
                    <Text color="subtle"> · </Text>
                    <Text color={hitColor(tu.hit)}>{String(Math.round(tu.hit * 100)).padStart(3)}%</Text>
                    <Text color="subtle">
                      {'  '}
                      {tu.steps}
                      {L('requests')} · {L('tools')} {tu.tools}
                    </Text>
                  </Text>
                  <Text color="subtle">
                    {(tu.durationMs / 1000).toFixed(1)}s · {L('hooks')} {k(tu.hookChars)}
                  </Text>
                </Box>
              ))}
          </Box>
        </Box>
      )
    }

    // 개요
    const cats = snap.categories.filter(c => c.kind === 'used' && c.tokens > 0).sort((a, b) => b.tokens - a.tokens)
    const rate = lastStep ? hitRate(lastStep) : 0
    const breaks: Record<string, number> = {}
    for (const s of main) if (s.brk) breaks[s.brk] = (breaks[s.brk] ?? 0) + 1
    const last = turnList[turnList.length - 1]
    const left = lastStep ? ttlMs(cacheTtl) - (now - lastStep.at) : 0
    const eta = compactEta(turnList, snap.autoCompact)
    const ctxNow = lastStep ? lastStep.input + lastStep.cacheRead + lastStep.cacheWrite + lastStep.output : snap.total
    const turnReqs = lv ? main.filter(s => s.at >= lv.turnStartedAt).length : 0
    // 창 전체 기준 게이지: 상시 로드 | 대화 | 빈칸, 자동 컴팩션 기준선은 │
    const convTokens = cats.find(c => c.name === 'Messages')?.tokens ?? 0
    const baseCats = cats.filter(c => c.name !== 'Messages')
    const baseTokens = Math.max(0, ctxNow - convTokens)
    const g = gauge(ctxNow, convTokens, snap.window, snap.autoCompact, Math.max(20, width - 2))

    return (
      <Box flexDirection="column" gap={1}>
        {header}

        <Box flexDirection="column">
          {title(L('now'))}
          {lv ? (
            <Text>
              <Text color="claude">● </Text>
              <Text bold>{lv.tool ? lv.tool.replace(/^mcp__/, '') : L('running')}</Text>
              <Text color="subtle">
                {lv.tool ? ` ${clock(now - lv.toolStartedAt)}` : ''} · {L('lastTurn')} {clock(now - lv.turnStartedAt)} · {turnReqs}
                {L('requests')}
                {lv.agents ? ` · ${L('agentsRunning')} ${lv.agents}` : ''}
              </Text>
            </Text>
          ) : (
            <Text color="subtle">
              {L('idle')}
              {lastStep ? ` · ${L('lastReply')} ${clock(now - lastStep.at)} ${L('ago')}` : ''}
            </Text>
          )}
        </Box>

        <Box flexDirection="column">
          {title(L('context'), `${k(ctxNow)} / ${k(snap.window)} · ${Math.round((ctxNow / snap.window) * 100)}%`)}
          <Text>
            <Text color="inactive">{'█'.repeat(g.base)}</Text>
            <Text color="claude">{'█'.repeat(g.conv)}</Text>
            <Text color="subtle">{'░'.repeat(g.before)}</Text>
            {g.mark >= 0 && <Text color="warning">│</Text>}
            <Text color="subtle">{'░'.repeat(g.after)}</Text>
          </Text>
          <Text>
            <Text color="inactive">■</Text> {L('baseLoad')} <Text color="subtle">{k(baseTokens)}</Text>
            {'   '}
            <Text color="claude">■</Text> {L('conversation')} <Text color="subtle">{k(convTokens)}</Text>
            {'   '}
            {snap.autoCompact ? (
              <Text>
                <Text color="warning">│</Text> {L('compaction')} <Text color="subtle">
                  {k(snap.autoCompact)} ({k(Math.max(0, snap.autoCompact - ctxNow))} {L('left')}
                  {eta !== null ? ` · ~${eta}${L('turnsUnit')}` : ''})
                </Text>
              </Text>
            ) : (
              <Text color="subtle">{L('compactOff')}</Text>
            )}
          </Text>
          <Text color="subtle" wrap="truncate-end">
            {'  '}
            {baseCats.map(c => `${c.name} ${k(c.tokens)}`).join(' · ')}
          </Text>
        </Box>

        <Box flexDirection="column">
          {title(L('cache'), spark(main.slice(-24).map(hitRate)))}
          {lastStep ? (
            <Box flexDirection="row" gap={2}>
              <Text bold color={hitColor(rate)}>
                {Math.round(rate * 100)}%
              </Text>
              <Text color="subtle">
                {L('read')} {k(lastStep.cacheRead)} · {L('write')} {k(lastStep.cacheWrite)} · {L('fresh')} {k(lastStep.input)} ·{' '}
                {L('output')} {k(lastStep.output)}
              </Text>
            </Box>
          ) : (
            <Text color="subtle">{L('noRequest')}</Text>
          )}
          {lastStep &&
            (left > 0 ? (
              <Text>
                <Text color={left > 60000 ? 'success' : 'warning'}>⏱ {clock(left)}</Text>
                <Text color="subtle">
                  {' '}
                  {L('cacheKeep')} · TTL {cacheTtl}
                  {cacheTtl === '5m' ? ` (${L('ttlGuess')})` : ''}
                </Text>
              </Text>
            ) : (
              <Text color="warning">⏱ {L('cacheExpired')}</Text>
            ))}
          {Object.keys(breaks).length > 0 && (
            <Text color="subtle">
              {L('breaks')}{' '}
              {Object.entries(breaks)
                .map(([cause, n]) => `${L(`brk_${cause}` as Key)} ${n}`)
                .join(' · ')}
            </Text>
          )}
        </Box>

        {last && (
          <Box flexDirection="column">
            {title(
              L('lastTurn'),
              `${L('score')} ${L('sessionScore')} ${Math.round(turnList.reduce((a, x) => a + (x.score ?? 100), 0) / turnList.length)}`,
            )}
            <Text>
              <Text bold color={scoreColor(last.score ?? 100)}>
                {last.score ?? 100}
              </Text>
              <Text color="subtle">
                {'  '}
                {(last.reasons ?? []).length ? last.reasons.map(r => L(`r_${r}` as Key)).join(' · ') : L('clean')}
              </Text>
            </Text>
            <Text color="subtle">
              {last.steps}
              {L('requests')} · {(last.durationMs / 1000).toFixed(1)}s · {L('tools')} {last.tools} · {L('hooks')} {k(last.hookChars)}
              {L('chars')}
            </Text>
          </Box>
        )}
      </Box>
    )
  })
}
