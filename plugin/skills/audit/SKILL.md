---
name: audit
disable-model-invocation: true
description: Audit the global scope (~/.claude skills, agents, plugins, MCP servers, hooks) against real session usage, show it in the local dashboard, archive what is unused or narrowly used into the registry and connect it only where it is used. Also trims unused local items in existing repos. Run right after installing the plugin, or on request.
---

# harnist audit — slim the global scope

The goal: only things every repo really uses should ride along in every session. Everything else lives in the registry as modules and is connected only to the repos that use it. Nothing is deleted — items leaving the global scope go to the registry or to `.trash/`.

```bash
H="${HARNIST_HOME:-$(cat ~/.claude/.harnist/home 2>/dev/null || echo ~/.claude/plugins/marketplaces/harnist)}"
```

## 1. Dashboard

Start `python3 "$H/harnist.py" view` in the background and open `http://127.0.0.1:8765/#audit`. Share the address first so the user can follow along. The same data as text:

```bash
python3 "$H/harnist.py" usage --days 90
```

Lead with the quality numbers at the top of the Audit tab: the skill list budget (are descriptions being truncated?) and per-repo fit (how much of what is loaded each repo actually uses). Tokens and cost are secondary.

## 2. Read the verdicts

| Verdict | Meaning | Suggestion |
| --- | --- | --- |
| Unused | never called in the window | archive to the registry, remove from global (no `--attach`) |
| Narrow | used in 1–2 repos | archive and connect only those repos (`--attach <repo>`) |
| Common | used in 3+ repos | keep global |
| No calls | a plugin with no recorded calls | ask the user what it is for |
| Always runs | hooks and hook-only plugins, not measurable by calls | report only |

Verdicts are evidence, not orders. Ask the user first about seasonal tools used outside the window, helper skills called by other skills, and anything installed so recently it has no history yet. Handle the largest always-on character counts first — those characters ride in every session.

## 3. Archive

Show the plan for each item (similar items may be grouped into one question), get consent, then run:

```bash
python3 "$H/harnist.py" demote skill:<name> --to domain/<module> --attach <repo> --dry-run
python3 "$H/harnist.py" demote skill:<name> --to domain/<module> --attach <repo>
```

- Kinds: `skill:`, `agent:`, `plugin:<name@marketplace>`, `mcp:`, and `module:domain/<name>` for harnist global modules.
- Group items with one purpose into the same `--to` module, for example image and video tools into `domain/media`.
- Skills leave a stub with zero always-on cost, so `/name` keeps working; `harnist recall` brings one back to a repo or to global.
- demote refuses MCP configs with `env` or `headers` so secrets never land in the registry. Build that module by hand with `${ENV_VAR}` values and tell the user to run `claude mcp remove <name> -s user`.
- If generation stops in a connected repo, finish it there with `/harnist:sync`.

## 4. Trim existing repos

For each repo in the dashboard's per-repo list (or `usage --json`):

- **Unused local items** — skills, agents or MCP in that repo's `.claude/` not used in the window. If harnist manages the repo, `harnist detach <module> <repo>` and generate. If they are hand-made, do not delete; with consent, move them into a project module or to `.trash/`.
- **Global items it used** — check that the ones archived in step 3 were connected here with `--attach`.

## 5. Finish

```bash
python3 "$H/harnist.py" audit --mark
```

This records the current global state as audited. From then on the SessionStart hook mentions, at most once a day, only global items added after this point. Report briefly: what left the global scope and how much always-on text it removed, and what was connected per repo. Changes apply after a session restart.
