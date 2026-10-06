---
name: init
disable-model-invocation: true
description: Set up a harnist harness in a new project folder (or a repo without one). Read the project, connect the shared registry modules it actually needs, design project-only agents and skills for the gaps as project modules, and generate .claude/. Also edits an existing harness.yaml.
---

# harnist init — set up a project harness

Do two things together: design agents and skills that fit this project, and keep the result connected to the shared registry as modules instead of a standalone `.claude/`. Connect what already exists; create only what is missing.

The engine lives in the harnist repo, not in the plugin. Call it like this:

```bash
H="${HARNIST_HOME:-$(cat ~/.claude/.harnist/home 2>/dev/null || echo ~/.claude/plugins/marketplaces/harnist)}"
python3 "$H/harnist.py" <command>
```

## 1. Read the project

Read the README and docs, the languages and build tools, `git log --oneline -20`, and any existing `.claude/` and CLAUDE.md. If `harness.yaml` already exists, go to step 7 (edit mode).

Do not throw away hand-made agents or skills in an existing `.claude/`. In step 6, move them into project modules and ask the user before adopting with `--adopt`.

## 2. Confirm the purpose

If the folder is empty or the intent is not clear from the code, ask the user one question at a time, at most three:

1. What the project builds and what the output looks like
2. Which work will repeat (research, implementation, review, release, docs, operations)
3. What it shares with other repos (team rules, knowledge base, data home)

## 3. Look at the main system

```bash
python3 "$H/harnist.py" list            # shared registry modules
python3 "$H/harnist.py" scan            # what other repos use
python3 "$H/harnist.py" usage --json    # which global items each repo actually called
```

Use real usage as evidence. Skills, plugins and MCP servers that similar repos actually called are strong candidates. Items that are installed but never used are not. Modules archived from the global scope by an audit also show up in `list`; connect them if this project needs them.

- **base modules**: things every repo of this kind needs regardless of purpose.
- **domain modules**: modules whose description matches the repeating work from step 2.
- Give the user one line of evidence per chosen module. When in doubt, leave it out — adding later is cheap.

## 4. Design for the gaps

Create something new only for repeating work no chosen module covers.

| Form | When | Example |
| --- | --- | --- |
| Agent | needs its own context, runs in parallel, or suits a different model | per-country analysis worker, independent reviewer |
| Skill | a procedure with order, rules or tool usage | release procedure, scoring method |
| claude_md | a short rule that always applies | output location, things not to do |

Start with at most three agents and three skills. Adding what turns out to be missing is cheaper than removing what missed. Group agents and skills with one purpose into one module; usually `project/<repo-name>` is enough.

## 5. Write the project module

```
.harnist/modules/project/<name>/
  module.yaml
  agents/<agent>.md
  skills/<skill>/SKILL.md (+ scripts/, references/)
```

```yaml
name: project/<name>
layer: project
description: one line — shown in scan and the dashboard
requires: [domain/...]
agents: [<agent>]
skills: [<skill>]
claude_md: |
  Triggers and rules: which agent or skill to use for what.
```

Agent files need `name`, `description` (make it clear when to call it) and `model` in the frontmatter. Skill descriptions should contain trigger phrases a user would actually type. Inside a skill, refer to its own files as `.claude/skills/<skill>/...` relative to the repo root; promotion rewrites these paths for the install location.

## 6. Manifest and generate

```bash
python3 "$H/harnist.py" init . --modules base/... domain/...
```

Add the project module to the generated `harness.yaml`, plus `spawn` (concurrency limit, model routing, rules), `teams` and `overrides.claude_md` (a repo-specific notes file) if needed. The format is in the manifest section of `$H/README.md`.

```bash
python3 "$H/harnist.py" generate --dry-run   # summarize the plan for the user
python3 "$H/harnist.py" generate
python3 "$H/harnist.py" check
```

If generate stops on an unmanaged file, do not overwrite it. Show it to the user, make sure its content moved into a project module, then run with `--adopt`.

## 7. Edit mode (harness.yaml exists)

Add or remove modules in `harness.yaml`; change agent and skill content under `.harnist/modules/project/...`; then run `generate`. Never edit generated files under `.claude/` directly.

## 8. Report

Finish with three short points:

1. What went in as base, domain and project-only
2. New agents and plugins load after the session restarts; new plugins show an install prompt at startup
3. `/harnist:view` shows the map; `/harnist:promote` shares a project module once another repo needs it
