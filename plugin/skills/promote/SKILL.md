---
name: promote
disable-model-invocation: true
description: Move this repo's project module (its own agents and skills) into the shared registry as a domain or base module so other repos can use it.
---

# harnist promote — share a project module

```bash
H="${HARNIST_HOME:-$(cat ~/.claude/.harnist/home 2>/dev/null || echo ~/.claude/plugins/marketplaces/harnist)}"
```

## 1. Check that it generalizes

Read the module and show the user anything tied to this repo:

- repo names, local paths and project-specific vocabulary in agent or skill text
- data paths that assume the repo root (`data/...`). Other repos do not have them; add a `rewrites` rule that points to the original location
- rules that only apply here. Keep those in this repo's `overrides.claude_md` instead of promoting them

## 2. Promote

Pick a name that shows layer and purpose, for example `domain/ios-release` or `base/commit-rules`.

```bash
python3 "$H/harnist.py" promote project/<name> --to domain/<new-name>
python3 "$H/harnist.py" generate
```

`promote` moves the module into the shared registry, adds a rewrite rule for `.claude/(skills|agents)/` paths, and renames references in this repo's `harness.yaml` and other project modules. It refuses while the module still depends on another project module.

## 3. Finish

- To use it elsewhere, add the new name to that repo's `harness.yaml` and run `generate` (or `/harnist:init` in edit mode there).
- If every session should have it, add it to the global manifest. The user scope only takes skills, agents and the CLAUDE.md block.
- The module now lives in the registry. Tell the user to back it up or commit it wherever they keep their registry.
