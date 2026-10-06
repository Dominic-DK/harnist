---
name: view
disable-model-invocation: true
description: Open the harnist dashboard locally — module × project map, layered assembly per repo, audit of global items by real usage, measurement of session startup and tokens, and one-click actions for repos without a harness.
---

# harnist view — local dashboard

Start the server in the background (use Bash `run_in_background`):

```bash
H="${HARNIST_HOME:-$(cat ~/.claude/.harnist/home 2>/dev/null || echo ~/.claude/plugins/marketplaces/harnist)}"
python3 "$H/harnist.py" view --open
```

The default address is `http://127.0.0.1:8765/`. It looks for `harness.yaml` up to six levels deep under `--root` (default `~/Documents/github`, or `HARNIST_ROOT`). If the port is busy, check whether harnist is already running there with `curl -s localhost:8765/api/state` and just share the address; otherwise pick another `--port`.

On the first start with no measurement on record, it measures a baseline once (two `claude -p` runs, a small cost). The Audit tab has the full check, recommendations and re-measurement; the right-hand panel of the Map tab connects modules, runs an agent design or opens a terminal for repos without a harness. `python3 "$H/harnist.py" demo` shows the same dashboard on fake data.

The page reloads from disk on every visit and on "Refresh", so there is no need to restart the server after `generate`. Give the user the address and one line on reading it: a filled dot means declared directly, a ring means pulled in as a dependency, orange means something needs attention.
