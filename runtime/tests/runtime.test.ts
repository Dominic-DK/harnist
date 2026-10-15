import { describe, expect, mock, test } from 'claude-code/testing'

import { parseConfig, roleFor, spawnDecision, toolBan, trimAttachment, trimDeferred, trimMcp, trimMemory, trimSkills } from '../hooks/rules'

const SKILLS = `The following skills are available for use with the Skill tool:

- aside-browser: Read when you need a browser.
- memory-bank:search-conversations: Search previous conversations
  across sessions
- loop
- insane-search:insane-search: 막힌 사이트 검색`

const MCP = `# MCP Server Instructions

The following MCP servers have provided instructions for how to use their tools and resources:

## askew
Askew lets you run Shortcuts.

## claude.ai Claude Docs
Docs: living docs.
`

const DEFERRED = `The following deferred tools are now available via ToolSearch:
CronCreate
mcp__askew__askew_run
mcp__claude_ai_Claude_Docs__batch
WebFetch`

const MEMORY = `Codebase and user instructions are shown below.

Contents of /Users/x/.claude/CLAUDE.md (user's private global instructions for all projects):

global rules

Contents of /Users/x/repo/CLAUDE.md (project instructions, checked into the codebase):

repo rules
`

const CFG = parseConfig(JSON.stringify({
  spawn: { maxConcurrent: 2, deny: ['statusline-setup'] },
  roles: {
    Explore: { model: 'haiku', skills: [], mcp: [], memory: false, denyTools: ['Write', 'Edit'] },
    'general-purpose': { skills: ['memory-bank:*', 'loop'], mcp: ['askew'], memory: ['/repo/'] },
    '*': { mcp: [] },
  },
}))

describe('runtime', () => {
  test('스킬 목록은 항목 단위로 거르고, 이어지는 줄도 같이 남긴다', async () => {
    const out = trimSkills(SKILLS, ['memory-bank:*', 'loop'])!
    expect(out).toContain('across sessions')
    expect(out).toContain('- loop')
    expect(out).not.toContain('aside-browser')
    expect(out.startsWith('The following skills')).toBe(true)
    expect(trimSkills(SKILLS, [])).toBeNull()
  })

  test('MCP 안내와 지연 도구는 서버 이름으로 거른다 (표기 차이 무시)', async () => {
    const out = trimMcp(MCP, ['claude.ai Claude Docs'])!
    expect(out).toContain('living docs')
    expect(out).not.toContain('Askew')
    expect(trimMcp(MCP, [])).toBeNull()
    const d = trimDeferred(DEFERRED, ['askew'])
    expect(d).toContain('mcp__askew__askew_run')
    expect(d).not.toContain('Claude_Docs')
    expect(d).toContain('WebFetch')
  })

  test('CLAUDE.md 는 파일 블록 단위로 남기거나 통째로 뺀다', async () => {
    expect(trimMemory(MEMORY, false)).toBeNull()
    const out = trimMemory(MEMORY, ['/repo/'])!
    expect(out).toContain('repo rules')
    expect(out).not.toContain('global rules')
    expect(trimMemory(MEMORY, true)).toBe(MEMORY)
  })

  test('역할 고르기와 생성 판정', async () => {
    expect(roleFor(CFG, 'Plan')).toEqual({ mcp: [] })
    expect(spawnDecision(CFG, 'statusline-setup', undefined, 0).deny).toContain('금지')
    expect(spawnDecision(CFG, 'Explore', undefined, 2).deny).toContain('상한')
    expect(spawnDecision(CFG, 'Explore', undefined, 0).model).toBe('haiku')
    expect(spawnDecision(CFG, 'Explore', 'opus', 0).model).toBeUndefined()
  })

  test('도구 거부: 금지 목록과 열리지 않은 MCP', async () => {
    const ex = roleFor(CFG, 'Explore')!
    expect(toolBan(ex, 'Edit')).toContain('Edit')
    expect(toolBan(ex, 'Read')).toBeUndefined()
    expect(toolBan(ex, 'mcp__askew__askew_run')).toContain('askew')
    expect(toolBan(roleFor(CFG, 'general-purpose')!, 'mcp__askew__askew_run')).toBeUndefined()
    expect(trimAttachment('environment', 'x', ex)).toBeUndefined()
  })

  test('설정이 없으면 아무것도 바꾸지 않고 /runtime 이 그렇게 알린다', async ($, on) => {
    mock.store(on)
    on('agent.spawn', async ($, e) => ({ model: e.model ?? 'parent', agentId: 'a1' }))
    on('agent.list', async () => [])
    on('prompt.attachment', async ($, e) => ({ text: (e as { text: string }).text }))
    on('session.cwd', async () => '/nowhere')
    const r = await $.command.run({ command: 'runtime' })
    expect(r.text).toContain('설정 없음')
  })
})
