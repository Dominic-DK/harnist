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

Install: `/plugin install runtime@harnist`. Design notes and measurements: [`docs/design/mods-runtime.md`](../docs/design/mods-runtime.md).
