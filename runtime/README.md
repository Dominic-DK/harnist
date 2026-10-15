# runtime — load each subagent by its role

A Claude Code mod (2.1.287+). One JSON file per repo says what each subagent type gets; the main session is left alone.

```json
{
  "spawn": { "maxConcurrent": 3, "deny": ["Plan"] },
  "roles": {
    "Explore": { "model": "haiku", "skills": [], "mcp": [], "memory": false, "denyTools": ["Write", "Edit"] },
    "general-purpose": { "skills": ["memory-bank:*"], "mcp": ["askew"], "memory": ["/CLAUDE.md"] },
    "*": { "mcp": [] }
  }
}
```

Put it at `<repo>/.claude/harnist-runtime.json` (or `~/.claude/harnist-runtime.json` for every repo).

| Key | Effect |
| --- | --- |
| `skills` | keep only these entries in the subagent's skill list (`x*` is a prefix); `[]` drops the list |
| `mcp` | keep only these servers' instructions and deferred tools; calls to other servers' tools are denied |
| `memory` | `false` drops CLAUDE.md for the subagent; a list of path fragments keeps only those files |
| `denyTools` | tools this role may not call |
| `model` | model for this subagent type unless Claude picked one |
| `spawn.maxConcurrent`, `spawn.deny` | refuse a spawn over the limit, or of a banned type |

`/runtime` shows the rules in force and how many characters were trimmed this session; `/runtime reload` rereads the file.

## Operating rules

- **Change role rules rarely.** Trimming changes the front of each subagent's request, so the first spawn after any change rewrites that role's prompt cache. In a measured run the trimmed subagents were 28–35% smaller, but a warm untrimmed run still cost less than the first trimmed one. Settle a repo's rules once and leave them.
- **Keep the main session untouched.** The mod only acts on subagents; the main loop's cache stays stable whatever you set.
- **Check with `/runtime` after editing**, then `/runtime reload` — a broken file disables the mod rather than half-applying it.
- **Separate workers get their own file.** `harnist profile` passes `HARNIST_RUNTIME_CONFIG`, which wins over the repo's file, so a research worker and a review worker in the same repo can trim differently.

Config lookup order: `HARNIST_RUNTIME_CONFIG`, `<repo>/.claude/harnist-runtime.json`, `~/.claude/harnist-runtime.json`.

Install: `/plugin install runtime@harnist`. Design notes and measurements: [`docs/design/mods-runtime.md`](../docs/design/mods-runtime.md).
