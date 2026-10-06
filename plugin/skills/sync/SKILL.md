---
name: sync
disable-model-invocation: true
description: Bring a repo's harness back in line with its harness.yaml and sources. Explain each drift, move hand edits of generated files back into their source, then regenerate. Also for the global (~/.claude) layer.
---

# harnist sync — regenerate

```bash
H="${HARNIST_HOME:-$(cat ~/.claude/.harnist/home 2>/dev/null || echo ~/.claude/plugins/marketplaces/harnist)}"
python3 "$H/harnist.py" check
```

Handle each kind of line that `check` prints:

| Line | Meaning | Action |
| --- | --- | --- |
| drift create / update / delete | the manifest or a source changed | run `generate` |
| lock change for a module | a source repo's skills or agents changed | look at that repo's `git log`, tell the user what changed, run `generate` |
| lock change for a marketplace | a plugin marketplace updated | tell the user, run `generate` (only the lock changes) |
| conflict: edited by hand | a generated file was edited | follow the steps below |
| conflict: unmanaged file | a file harnist did not create sits at that path | show it to the user and use `--adopt` only with their consent |

Never overwrite a hand-edited generated file straight away. Compare it with the hash recorded in `.claude/.harnist/ledger.json` and show the user what changed. Move that change into the source — `.harnist/modules/...` for a project module, or the repo that `source` points to for a domain module — and only then run `generate --force`.

```bash
python3 "$H/harnist.py" generate
python3 "$H/harnist.py" check
```

For the global layer (`~/.claude`), run the same steps with `-m "$H/manifests/global/harness.yaml"` (or `~/.harnist/manifests/global/harness.yaml`). Tell the user that new agents and plugins load after a restart.
