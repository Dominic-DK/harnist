import { describe, expect, mock, test } from 'claude-code/testing'

import { pickLang, t } from '../hooks/i18n'
import { bar, breakCause, clock, compactEta, digest, fit, gauge, scoreTurn, hitRate, k, learnTtl, limitLabel, mcpServer, rebuild, rollup, spark, speed, usedKey } from '../hooks/stats'
import type { LoadedItem, Step } from '../types'

const ITEMS: LoadedItem[] = [
  { kind: 'skill', name: 'insane-search:insane-search', source: 'plugin:insane-search', tokens: 336, deferred: 0 },
  { kind: 'skill', name: 'loop', source: 'built-in', tokens: 30, deferred: 0 },
  { kind: 'mcp', name: 'claude.ai Google Drive', source: 'mcp', tokens: 0, deferred: 9000 },
  { kind: 'agent', name: 'Explore', source: 'built-in', tokens: 85, deferred: 0 },
  { kind: 'memory', name: '/Users/x/.claude/CLAUDE.md', source: 'User', tokens: 1291, deferred: 0 },
]
const step = (over: Partial<Step>): Step => ({
  turnId: 't', index: 0, model: 'opus', agentId: '', input: 2, output: 10, cacheRead: 900, cacheWrite: 98, at: 0, brk: '', ms: 0, ttft: 0, ...over,
})
const PANE = { title: 'Lens', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { bodyRows: 40 } } as any

describe('lens', () => {
  test('숫자 표시 도구', async () => {
    expect(Math.round(hitRate({ input: 6, cacheRead: 66227, cacheWrite: 35327 }) * 1000)).toBe(652)
    expect(hitRate({ input: 0, cacheRead: 0, cacheWrite: 0 })).toBe(0)
    expect(spark([0, 0.5, 1])).toBe('▁▅█')
    expect(bar(0.5, 4)).toBe('██')
    expect(bar(0.3, 2)).toBe('▋')
    expect(k(35329)).toBe('35.3k')
    expect(mcpServer('mcp__claude_ai_Google_Drive__search_files')).toBe('claude_ai_Google_Drive')
    expect(usedKey(ITEMS[2]!)).toBe('mcp:claude_ai_google_drive')
  })

  test('캐시 깨짐 원인을 가른다', async () => {
    expect(breakCause(undefined, { model: 'opus', at: 0 }, 0)).toBe('cold')
    expect(breakCause(step({ model: 'haiku' }), { model: 'opus', at: 1000 }, 0.1)).toBe('model')
    expect(breakCause(step({ at: 0 }), { model: 'opus', at: 6 * 60 * 1000 }, 0.1)).toBe('gap')
    expect(breakCause(step({ at: 0 }), { model: 'opus', at: 1000 }, 0.1)).toBe('other')
    expect(breakCause(step({}), { model: 'opus', at: 1000 }, 0.9)).toBe('')
  })

  test('실렸지만 안 쓴 개수 (메모리 파일 제외)', async () => {
    expect(fit(ITEMS, ['skill:loop', 'mcp:claude_ai_google_drive'])).toEqual({
      skill: { loaded: 2, used: 1 },
      mcp: { loaded: 1, used: 1 },
      agent: { loaded: 1, used: 0 },
    })
  })

  test('세션 기록을 요약하고 여러 세션을 합친다', async () => {
    const rows = [
      { v: 1, kind: 'session', cwd: '/w/app', model: 'opus' },
      { v: 1, kind: 'attachment', origin: { kind: 'hook', event: 'SessionStart' }, chars: 400 },
      { v: 1, kind: 'attachment', origin: { kind: 'hook', event: 'UserPromptSubmit' }, chars: 9999, replay: true },
      { v: 1, kind: 'attachment', origin: { kind: 'hook', event: 'UserPromptSubmit' }, chars: 100 },
      { v: 1, kind: 'step', agentId: '', input: 10, cacheRead: 0, cacheWrite: 990, brk: 'cold' },
      { v: 1, kind: 'step', agentId: '', input: 10, cacheRead: 990, cacheWrite: 0, brk: '' },
      { v: 1, kind: 'tool', tool: 'Read', ms: 200 },
      { v: 1, kind: 'tool', tool: 'mcp__open__q', ms: 800 },
      { v: 1, kind: 'skill', skill: 'loop' },
      { v: 1, kind: 'turn', durationMs: 4000 },
      { v: 1, kind: 'snapshot', detail: 'full', categories: [
        { name: 'Skills', kind: 'used', tokens: 500 }, { name: 'Messages', kind: 'used', tokens: 9000 }], items: ITEMS },
      { kind: 'breakdown' },
    ]
    const d = digest('s1', rows)!
    expect(d.cacheHit).toBe(0.495)
    expect(d.breaks).toEqual({ cold: 1 })
    expect(d.tools['mcp__open__q']).toEqual({ count: 1, ms: 800 })
    expect([d.hookStartChars, d.hookCharsPerTurn, d.avgTurnSec]).toEqual([400, 100, 4])
    expect(d.baseLoad).toBe(500)
    expect(d.idle.map(x => x.id)).toEqual(['skill:insane-search:insane-search', 'agent:Explore'])
    expect(digest('empty', [{ kind: 'breakdown' }])).toBeNull()

    const r = rollup([d, { ...d, id: 's2' }])
    expect(r.sessions).toBe(2)
    expect(r.tools[0]).toEqual(['Read', 2])
    expect(r.alwaysIdle[0]).toEqual({ id: 'skill:insane-search:insane-search', tokens: 336, sessions: 2, source: 'plugin:insane-search' })
  })

  test('언어 고르기', async () => {
    expect(pickLang('en', 'ko_KR.UTF-8')).toBe('en')
    expect(pickLang(undefined, 'ko_KR.UTF-8')).toBe('ko')
    expect(pickLang(undefined, undefined)).toBe('en')
    expect(t('en', 'overview')).toBe('Overview')
  })

  test('report 도구는 이 세션 요약을 JSON 으로 답한다', async ($, on) => {
    mock.store(on)
    on('session.usage', async () => ({ value: { startedAt: 0, context: { window: 1000000 }, rateLimits: [] } }))
    const r = await $.tool.call({ tool: 'mcp__lens__report', scope: 'session' } as any)
    expect(JSON.parse((r as any).text ?? (r as any).result).scope).toBe('session')
  })

  test('패널은 측정 전 안내와 탭 머리를 그리고, 언어 단추로 영어가 된다', async ($, on) => {
    mock.store(on)
    on('ui.open', async () => ({ value: { isPlaced: true } }))
    on('session.usage', async () => ({ value: { startedAt: 0, context: { window: 1000000 }, rateLimits: [] } }))
    await $.command.run({ command: 'lens', args: 'lang ko' })
    await $.command.run({ command: 'lens' })
    const ui = await $.ui.mount({ plugin: 'lens', surface: 'terminal', component: 'Pane', requestId: 'lens', props: PANE })
    expect(await ui.find({ type: 'Text', text: /첫 응답이 끝나면/ })).toBeDefined()
    expect(await ui.find({ key: 'tab-load' })).toBeDefined()
    await ui.press({ key: 'lang' })
    expect(await ui.find({ type: 'Text', text: /Measuring after/ })).toBeDefined()
    await ui.unmount()
  })
})

describe('lens rebuild', () => {
  test('같은 세션 기록으로 도구·턴·사용 항목을 다시 채운다', async () => {
    const r = rebuild([
      { v: 1, kind: 'tool', tool: 'Bash', ms: 100 },
      { v: 1, kind: 'tool', tool: 'Bash', ms: 300 },
      { v: 1, kind: 'tool', tool: 'mcp__open__q', ms: 50 },
      { v: 1, kind: 'skill', skill: 'loop' },
      { v: 1, kind: 'turn', turnId: 'a', durationMs: 2000, steps: 2, toolCount: 3, t: 5 },
      { v: 1, kind: 'step', turnId: 'a', index: 0, model: 'opus', agentId: '', input: 1, output: 2, cacheRead: 3, cacheWrite: 4, t: 9 },
      { v: 1, kind: 'attachment', origin: { kind: 'hook', event: 'SessionStart' }, chars: 70, replay: true },
      { v: 1, kind: 'attachment', origin: { kind: 'hook', event: 'SessionStart' }, chars: 30 },
      { kind: 'breakdown' },
    ])
    expect(r.toolStats.find(x => x.tool === 'Bash')).toEqual({ tool: 'Bash', count: 2, ms: 400, failed: 0 })
    expect(r.used.sort()).toEqual(['mcp:open', 'skill:loop'])
    expect(r.turns[0]?.tools).toBe(3)
    expect(r.steps[0]?.at).toBe(9)
    expect(r.startHookChars).toBe(30)
    expect(r.snapshot).toBeNull()
  })
})

describe('lens 실시간 계산', () => {
  test('시계·한도 이름·컴팩션 예상·속도·TTL 학습', async () => {
    expect(clock(185000)).toBe('3:05')
    expect(clock(3725000)).toBe('1:02:05')
    expect(limitLabel('five_hour')).toBe('5h')
    expect(limitLabel('seven_day_opus')).toBe('7d')
    expect(compactEta([{ ctx: 100 }, { ctx: 200 }, { ctx: 300 }], 1000)).toBe(7)
    expect(compactEta([{ ctx: 300 }], 1000)).toBeNull()
    expect(compactEta([{ ctx: 100 }, { ctx: 200 }], 0)).toBeNull()
    expect(speed([{ output: 100, ms: 3000, ttft: 1000 }, { output: 300, ms: 4000, ttft: 1000 }])).toEqual({ tokPerSec: 80, ttftSec: 1 })
    expect(speed([])).toBeNull()
    const six = 6 * 60 * 1000
    expect(learnTtl({ at: 0 }, { at: six }, 0.95, '5m')).toBe('1h')
    expect(learnTtl({ at: 0 }, { at: six }, 0.1, '5m')).toBe('5m')
    expect(learnTtl({ at: 0 }, { at: 1000 }, 0.95, '5m')).toBe('5m')
  })
})

describe('lens 게이지', () => {
  test('상시 로드·대화·빈칸·컴팩션 기준선 칸 수', async () => {
    // 1M 창, 100칸: 상시 30k → 3칸, 대화 320k → 32칸, 기준선 967k → 97번째 칸
    expect(gauge(350000, 320000, 1000000, 967000, 100)).toEqual({ base: 3, conv: 32, before: 62, mark: 62, after: 2 })
    expect(gauge(350000, 320000, 1000000, 0, 100)).toEqual({ base: 3, conv: 32, before: 65, mark: -1, after: 0 })
    // 기준선을 이미 넘었으면 표시하지 않는다
    expect(gauge(990000, 950000, 1000000, 967000, 100).mark).toBe(-1)
  })
})

describe('lens 점수', () => {
  test('감점 사유와 점수', async () => {
    const zero = { aborted: false, error: false, failed: 0, denied: 0, asked: 0, repeat: 0, compacted: 0 }
    expect(scoreTurn(zero)).toEqual({ score: 100, reasons: [] })
    expect(scoreTurn({ ...zero, failed: 2, repeat: 1 })).toEqual({ score: 65, reasons: ['failed', 'repeat'] })
    expect(scoreTurn({ ...zero, aborted: true, error: true, failed: 9, denied: 5, asked: 9, repeat: 1, compacted: 1 }).score).toBe(0)
  })
})
