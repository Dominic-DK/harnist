---
name: lens-review
description: Analyze lens measurements — what this session and recent sessions mostly do, where tokens and cache go — and propose harness fixes in priority order. Use for "lens review", "analyze this session", "why did the cache break", "what is eating my context", "lens 분석", "세션 분석해줘", "캐시 왜 깨졌어", "컨텍스트 뭐가 먹어".
---

# lens-review

Analyze this session and recent sessions from what the lens mod measured, and propose fixes to the harness (CLAUDE.md, skills, plugins, MCP servers, hooks) in priority order. Start from the numbers, not from guesses.

## 1. Get the data

Call the `mcp__lens__report` tool twice (load it through tool search first if it is deferred):

- `{ "scope": "session" }` — this session: requests, cache hit, what broke the cache (`breaks`), calls and time per tool, skills and agents used, base load (`baseLoad`, the always-on tokens without the conversation) and its `categories`, hook injections, and large items that were loaded but not used (`idle`).
- `{ "scope": "recent", "limit": 20 }` — recent sessions. `summary.tools` and `summary.skillsAndAgents` show what the work mostly is, `summary.alwaysIdle` lists items loaded in session after session but never used (ranked by tokens × sessions), and `summary.breaks` totals cache-break causes.

If the tool does not exist, the lens mod is not loaded: tell the user to check that `lens` is enabled in `/plugin`, and stop. The raw records are `~/.claude/lens/<session id>.jsonl` (structure and sizes only, no message text).

## 2. Diagnose

Go in this order. When the numbers are small or there are few sessions, say so and weaken the conclusion. Before recommending removal of anything, check its usage over a longer window when you can (for example `harnist usage --days 90` or `harnist skills`): lens only sees sessions it was loaded in.

1. **What the work is.** From the tool mix (Read / Edit / Bash / search / MCP), the skills and agents used, requests per turn and turn time, describe the work in one or two sentences — for example "mostly exploration, few edits" or "external MCP calls take most of the time".
2. **Always-on cost.** Point out the large categories in the base load (System tools, Skills, Memory files, MCP server instructions). Items in `idle` / `alwaysIdle` with large tokens × sessions are the candidates. Hook injections (`hookStartChars`, `hookCharsPerTurn`) are paid every session and every turn, so call them out separately.
3. **Cache.** If the average hit is low or `breaks` is not empty, read each cause:
   - `cold` — the first request; expected.
   - `model` — the model changed mid-session and the cache was thrown away.
   - `gap` — more than five minutes idle; the cache likely expired.
   - `other` — the system prompt or the start of the conversation changed. Suspect a hook that injects text that changes every turn, or a CLAUDE.md edited during the session.

## 3. Propose fixes

Give 3–6 fixes, biggest effect first. For each: **what / where / expected effect (tokens, hit rate) / the numbers behind it**. Common ones:

- **Unused skill descriptions.** Loaded every session but not used → `skillOverrides: name-only` in `settings.json` (the name stays and can still be invoked). With harnist: `harnist skills --name-only <name>`, or `harnist demote` to connect it only to the repos that use it.
- **Unused MCP servers.** If the schemas are already deferred, only the instructions remain, so rank it lower. If a server is never used, suggest disabling it or connecting it per repo.
- **Large CLAUDE.md.** Move procedures and long tables that most turns do not need into skills, or shorten them. Show which file costs how many tokens.
- **Hook injections.** A hook that adds hundreds of characters every turn can be made conditional or shorter. Name the event (UserPromptSubmit and so on).
- **Cache breaks.** Avoid switching models mid-session, and move hooks that put changing values near the start of the prompt later or remove them. With long idle gaps, consider the one-hour cache TTL.

## 4. Apply

After the proposal, ask the user which fixes to apply, and change only those. Back up settings files before editing them, and leave a note of what changed and how to undo it. Suggest checking the effect in the next sessions' lens numbers.
