"""harnist demo — 가짜 데이터로 대시보드를 띄운다.

실제 ~/.claude 나 레포는 건드리지 않는다. 임시 폴더에 가짜 프로젝트·레지스트리·전역 설정·세션 기록·측정 기록을
만들고, 엔진이 그 폴더만 보도록 환경을 바꾼 뒤 읽기 전용 대시보드를 연다. README 캡처도 이 화면으로 만든다.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import harnist as H
from harnist import tr


def w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def skill(d: Path, name: str, desc: str, body: str = "") -> None:
    w(d / name / "SKILL.md", f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n{body or 'Demo skill.'}\n")


def agent(d: Path, name: str, desc: str, model: str = "sonnet") -> None:
    w(d / f"{name}.md", f"---\nname: {name}\ndescription: {desc}\nmodel: {model}\n---\n\nDemo agent.\n")


def build(root: Path) -> dict:
    ws, reg, home = root / "workspace", root / "registry", root / "home" / ".claude"

    # ---------------- 레지스트리 (공유 모듈) ----------------
    w(reg / "base/conventions/module.yaml", """layer: base
description: Team conventions — commit style, review rules, a shared reviewer agent
agents: [code-reviewer]
claude_md: |
  Use Conventional Commits. Run the tests before opening a PR. Ask code-reviewer for a second look on risky changes.
""")
    agent(reg / "base/conventions/agents", "code-reviewer", "Reviews a diff for correctness and risky changes before a PR.")
    w(reg / "domain/web-frontend/module.yaml", """layer: domain
description: Web frontend — UI polish skill and a browser automation MCP
requires: [base/conventions]
skills: [ui-polish]
mcp:
  playwright: {type: stdio, command: npx, args: ["@playwright/mcp@latest"]}
claude_md: |
  For visual changes, use ui-polish and check the page in a real browser through the playwright MCP.
""")
    skill(reg / "domain/web-frontend/skills", "ui-polish", "Tighten spacing, type scale and states of a UI component. Use for 'polish this page'.")
    w(reg / "domain/data-eng/module.yaml", """layer: domain
description: Data engineering — SQL tuning skill and a schema auditor agent
requires: [base/conventions]
skills: [sql-tuner]
agents: [schema-auditor]
""")
    skill(reg / "domain/data-eng/skills", "sql-tuner", "Explain and speed up slow SQL queries. Use for 'this query is slow'.")
    agent(reg / "domain/data-eng/agents", "schema-auditor", "Audits schema migrations for locking and data loss.")
    w(reg / "domain/mobile-release/module.yaml", """layer: domain
description: Mobile release — store submission checklist skill
skills: [store-release]
""")
    skill(reg / "domain/mobile-release/skills", "store-release", "Prepare an App Store / Play release: version bump, notes, screenshots.")
    w(reg / "domain/research/module.yaml", """layer: domain
description: Deep research skill (archived from global by an audit)
skills: [deep-research]
""")
    skill(reg / "domain/research/skills", "deep-research", "Multi-source research with citations. Use for 'research this'.")

    # ---------------- 워크스페이스 (프로젝트) ----------------
    projects = {
        "shop-web": ["base/conventions", "domain/web-frontend", "project/checkout-reviewer"],
        "data-pipeline": ["base/conventions", "domain/data-eng"],
        "mobile-app": ["base/conventions", "domain/mobile-release"],
    }
    for name, mods in projects.items():
        d = ws / name
        (d / ".git").mkdir(parents=True, exist_ok=True)
        extra = ""
        if name == "shop-web":
            extra = """spawn:
  max_parallel_agents: 4
  routing: {code-reviewer: opus}
teams:
  checkout:
    purpose: Ship checkout changes safely
    lead: checkout-reviewer
    members: [checkout-reviewer, code-reviewer, ui-polish]
"""
            m = d / ".harnist/modules/project/checkout-reviewer"
            w(m / "module.yaml", "layer: project\ndescription: Checks payment edge cases in this repo\nrequires: [base/conventions]\nagents: [checkout-reviewer]\n")
            agent(m / "agents", "checkout-reviewer", "Reviews checkout and payment code paths for edge cases.", "opus")
        w(d / "harness.yaml", "modules:\n" + "".join(f"  - {x}\n" for x in mods) + extra)
        w(d / "CLAUDE.md", f"# {name}\n\nHand-written notes stay outside the harnist block.\n")
    for name, files in {
        "marketing-site": {".claude/skills/seo-audit/SKILL.md": "---\nname: seo-audit\ndescription: Audit a page for SEO issues.\n---\n", "CLAUDE.md": "# marketing-site\n"},
        "ml-notebooks": {"CLAUDE.md": "# ml-notebooks\n"},
        "infra": {".mcp.json": json.dumps({"mcpServers": {"terraform": {"type": "stdio", "command": "terraform-mcp"}}})},
        "new-app": {},
    }.items():
        d = ws / name
        (d / ".git").mkdir(parents=True, exist_ok=True)
        for rel, text in files.items():
            w(d / rel, text)

    # ---------------- 가짜 전역 (~/.claude) ----------------
    gs = home / "skills"
    skill(gs, "translate-docs", "Translate documentation between languages while keeping code blocks and links intact. Use for 'translate this doc'.")
    skill(gs, "slack-digest", "Summarize a Slack channel export into a weekly digest with decisions, owners and open questions.")
    skill(gs, "pdf-report", "Turn a markdown analysis into a styled PDF report with cover page, table of contents and charts. Use for 'make a PDF report'.")
    skill(gs, "image-gen", "Compile a vague image request into a full prompt (subject, lens, light, palette, layout) and render it through an image backend. Use for 'make an image', 'poster', 'thumbnail', 'key art', 'sticker set', 'banner'.")
    w(gs / "deep-research/SKILL.md", "---\nname: deep-research\ndescription: (archived) Multi-source research\ndisable-model-invocation: true\nharnist-stub: domain/research\nharnist-origin: skill\n---\n\nStub.\n")
    agent(home / "agents", "test-writer", "Writes focused unit tests for a change.")
    agent(home / "agents", "perf-profiler", "Profiles hot paths and suggests optimizations with measurements.")
    plug = root / "plugins"
    skill(plug / "lint-kit/skills", "lint-fix", "Run the project's linters and fix what can be fixed automatically.")
    skill(plug / "design-pack/skills", "brand-colors", "Generate an accessible brand palette with light and dark variants.")
    skill(plug / "design-pack/skills", "icon-set", "Draw a consistent SVG icon set from a short list of names.")
    w(home / "plugins/installed_plugins.json", json.dumps({"plugins": {
        "lint-kit@acme": [{"installPath": str(plug / "lint-kit")}],
        "design-pack@acme": [{"installPath": str(plug / "design-pack")}]}}))
    w(home / "settings.json", json.dumps({
        "enabledPlugins": {"lint-kit@acme": True, "design-pack@acme": True},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "~/.local/bin/notify-start"}]}]},
    }))
    w(home.parent / ".claude.json", json.dumps({"mcpServers": {
        "notion": {"type": "http", "url": "https://mcp.notion.example/mcp"},
        "figma": {"type": "stdio", "command": "figma-mcp"}}}))

    # ---------------- 가짜 세션 기록 ----------------
    now = time.time()
    lines = []
    def use(repo: str, tool: dict, days_ago: float):
        lines.append((repo, tool, now - days_ago * 86400))
    for i, repo in enumerate(["shop-web", "data-pipeline", "mobile-app", "ml-notebooks"]):
        use(repo, {"name": "Skill", "input": {"skill": "translate-docs"}}, 2 + i)
        use(repo, {"name": "Agent", "input": {"subagent_type": "test-writer"}}, 1 + i)
    use("shop-web", {"name": "Skill", "input": {"skill": "slack-digest"}}, 3)
    use("shop-web", {"name": "Skill", "input": {"skill": "lint-kit:lint-fix"}}, 4)
    use("marketing-site", {"name": "Skill", "input": {"skill": "design-pack:brand-colors"}}, 6)
    for k in range(5):
        use("shop-web", {"name": "mcp__figma__get_frame", "input": {}}, 1 + k)
    use("infra", {"name": "mcp__notion__search", "input": {}}, 8)
    for n, (repo, tool, ts) in enumerate(lines):
        f = home / "projects" / f"-demo-{repo}" / f"s{n}.jsonl"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))
        w(f, json.dumps({"type": "assistant", "cwd": str(ws / repo), "timestamp": stamp,
                         "message": {"content": [{"type": "tool_use", **tool}]}}) + "\n")
        os.utime(f, (ts, ts))
    return {"workspace": ws, "registry": reg, "home": home, "plugins": plug}


def fake_lens(home: Path, ws: Path) -> None:
    """가짜 lens 기록 — 트레이스 탭이 비지 않게. 턴·요청·도구·서브에이전트·비용·점수를 고루 섞는다."""
    import random
    rnd = random.Random(7)
    d = home / "lens"
    d.mkdir(parents=True, exist_ok=True)
    base = int(time.time() * 1000) - 3 * 3600 * 1000
    plans = [("shop-web", "claude-opus-5-5[1m]", True, 6), ("data-pipeline", "claude-sonnet-5-5", True, 4),
             ("shop-web", "claude-haiku-5-5", False, 2), ("mobile-app", "claude-opus-5-5[1m]", True, 5)]
    for si, (repo, model, interactive, n_turns) in enumerate(plans):
        rows = [{"v": 1, "t": base, "kind": "session", "cwd": str(ws / repo), "model": model, "interactive": interactive}]
        rows.append({"v": 1, "t": base, "kind": "experiment", "id": "short-tone", "variant": "AB"[si % 2], "op": "replace", "section": "tone"})
        now, cost = base + si * 40 * 60 * 1000, 0.0
        for ti in range(n_turns):
            tid, start = f"t{si}-{ti}", now
            rows.append({"v": 1, "t": start, "kind": "turnStart", "turnId": tid, "agentId": "", "at": start})
            failed = []
            for k in range(rnd.randint(1, 6)):
                ttft = rnd.randint(900, 2600)
                ms = ttft + rnd.randint(800, 9000)
                read = 20000 + ti * 9000 if (k or ti) else 0
                write = rnd.randint(300, 4000) if read else 24000
                rows.append({"v": 1, "t": now + ms, "kind": "step", "turnId": tid, "index": k, "model": model, "agentId": "",
                             "input": 4, "output": rnd.randint(60, 1800), "cacheRead": read, "cacheWrite": write,
                             "at": now + ms, "ms": ms, "ttft": ttft, "brk": "" if read else "cold", "stop": "tool_use"})
                now += ms + 120
                for tool in rnd.sample(["Read", "Grep", "Edit", "Bash", "mcp__figma__get_frame"], rnd.randint(0, 2)):
                    tms = rnd.randint(200, 6000 if tool == "Bash" else 1500)
                    err = tool == "Bash" and rnd.random() < 0.25
                    if err:
                        failed.append(tool)
                    rows.append({"v": 1, "t": now + tms, "kind": "tool", "tool": tool, "start": now, "ms": tms, "agentId": "",
                                 "turnId": tid, "isError": err})
                    now += tms + 80
            if ti == 1:  # 서브에이전트 한 번
                aid, astart = f"a{si}", start + 1500
                rows.append({"v": 1, "t": astart, "kind": "turnStart", "turnId": f"s{si}", "agentId": aid, "at": astart})
                rows.append({"v": 1, "t": astart + 5200, "kind": "step", "turnId": f"s{si}", "index": 0, "model": "claude-haiku-5-5",
                             "agentId": aid, "input": 3, "output": 420, "cacheRead": 9000, "cacheWrite": 2100,
                             "at": astart + 5200, "ms": 5200, "ttft": 1100, "brk": "", "stop": "tool_use"})
                rows.append({"v": 1, "t": astart + 6400, "kind": "tool", "tool": "Grep", "start": astart + 5300, "ms": 1100,
                             "agentId": aid, "turnId": "", "isError": False})
                rows.append({"v": 1, "t": astart + 9800, "kind": "agentDone", "agentId": aid, "type": "Explore", "durationMs": 8300})
                now = max(now, astart + 9800)
            cost += rnd.uniform(0.02, 0.4) if "opus" in model else rnd.uniform(0.004, 0.05)
            reasons = (["failed"] if failed else []) + (["repeat"] if len(failed) > 1 else [])
            score = max(0, 100 - 10 * len(failed) - (15 if len(failed) > 1 else 0))
            rows.append({"v": 1, "t": now, "kind": "turn", "turnId": tid, "durationMs": now - start, "score": score,
                         "reasons": reasons, "hookChars": 240, "ctx": 30000 + ti * 9000})
            rows.append({"v": 1, "t": now + 50, "kind": "cost", "usd": round(cost, 4)})
            now += rnd.randint(20, 240) * 1000
        rows.append({"v": 1, "t": base, "kind": "snapshot", "detail": "full", "total": 31000, "window": 1000000, "categories": [
            {"name": "System prompt", "kind": "used", "tokens": 3900}, {"name": "System tools", "kind": "used", "tokens": 9800},
            {"name": "Skills", "kind": "used", "tokens": 3100}, {"name": "Messages", "kind": "used", "tokens": 14200}],
            "items": [{"kind": "skill", "name": "translate-docs", "source": "userSettings", "tokens": 140, "deferred": 0},
                      {"kind": "skill", "name": "slack-digest", "source": "userSettings", "tokens": 210, "deferred": 0}]})
        f = d / f"demo-session-{si}.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        os.utime(f, (time.time() - si * 600, time.time() - si * 600))


def fake_bench(home: Path, rows: list, root: Path) -> None:
    """가짜 측정 기록 — 기준선과 슬림화 뒤, 네트워크 지연 한 번을 섞어 '느려진 이유'까지 보이게 한다."""
    def probe(model, ctx, write, cost, local, net, plugins, api, tail, out, causes, skills=(41, 16800, 8000)):
        req = local + net + plugins
        return {"model": model, "ctx_tokens": ctx, "cache_write": write, "cache_read": ctx - write, "out_tokens": out,
                "cost_usd": cost, "to_request_s": round(req, 2), "first_token_s": round(req + api, 2), "end_s": round(req + api + tail, 2),
                "wall_s": round(req + api + tail + 0.1, 2), "skills_n": skills[0], "skills_chars": skills[1], "skills_budget": skills[2], "hooks_n": 9, "mcp_ok": ["figma"], "mcp_fail": [],
                "seg": {"local": local, "net": net, "plugins": plugins}, "causes": causes}
    sessions = {"30": {"days": 30, "sessions": 142, "agent_spawns": 380, "covered_days": 30},
                "90": {"days": 90, "sessions": 142, "agent_spawns": 380, "covered_days": 30}}
    base = {"at": "2026-10-01 09:12:04", "label": "baseline", "sessions": sessions, "static": H.static_summary(rows), "recs": [],
            "probes": {"user": probe("claude-opus-5-5[1m]", 33120, 21400, 0.1731, 1.62, 0.18, 1.31, 2.4, 0.9, 4, [], skills=(41, 16800, None)),
                       "agent": probe("claude-haiku-4-5", 29840, 15100, 0.0331, 1.48, 0.21, 1.27, 0.8, 2.1, 160, [])}}
    after = {"at": "2026-10-02 18:40:51", "label": "after", "sessions": sessions, "static": H.static_summary(rows),
             "recs": sorted(H.recommendations(rows, root) + [
                 {"item": f"listing:{n}", "kind": "listing", "name": n, "verdict": "미사용", "to": None, "attach": [], "chars": c,
                  "sessions": 0, "action": "name-only", "checked": True, "blocked": False, "origin": "builtin"}
                 for n, c in (("dataviz", 1436), ("claude-api", 1068), ("schedule", 369))], key=lambda x: -x["chars"]),
             "probes": {"user": probe("claude-opus-5-5[1m]", 25480, 15200, 0.1187, 0.94, 2.86, 1.25, 2.3, 0.8, 4,
                                      [{"kind": "net_timeout", "x": "Grove settings", "s": 2.65, "at": 2.9}, {"kind": "net", "x": "claudeai-mcp", "s": 0.41, "at": 0.9}], skills=(24, 9600, None)),
                        "agent": probe("claude-haiku-4-5", 27010, 10900, 0.0254, 0.88, 0.22, 1.24, 0.7, 2.0, 150, [], skills=(24, 9600, 8000))}}
    d = home / ".harnist" / "bench"
    d.mkdir(parents=True, exist_ok=True)
    (d / "20261001-091204-baseline.json").write_text(json.dumps(base))
    (d / "20261002-184051-after.json").write_text(json.dumps(after))
    (d / "baseline.json").write_text(json.dumps(base))


def run(port: int = 8766, open_browser: bool = False, where: str | None = None) -> None:
    root = Path(where).expanduser().resolve() if where else Path(tempfile.mkdtemp(prefix="harnist-demo-"))
    paths = build(root)
    # 엔진이 가짜 세계만 보게 한다
    os.environ["HARNIST_CLAUDE_HOME"] = str(paths["home"])
    os.environ["HARNIST_PLUGIN_HOME"] = str(root / "no-marketplaces")
    H.DEFAULT_REGISTRY = paths["registry"]
    H.GLOBAL_MANIFEST = root / "manifests/global/harness.yaml"
    H.TEMP_PREFIXES = ()
    for name in ("shop-web", "data-pipeline", "mobile-app"):
        H.main(["generate", "-m", str(paths["workspace"] / name / "harness.yaml")])
    # 원본이 바뀐 상황 하나 — 지도에 '드리프트' 가 보이게
    sk = paths["registry"] / "domain/data-eng/skills/sql-tuner/SKILL.md"
    sk.write_text(sk.read_text() + "\nAlso check the query plan for sequential scans.\n")
    rows = H.usage_report(90)
    w(paths["home"] / ".harnist/audit.json", json.dumps({"at": "2026-10-01 09:20", "items": [
        i for i in H.item_ids(H.global_items()) if i != "skill:image-gen"]}))  # image-gen = 점검 뒤 새로 붙은 전역 항목
    fake_bench(paths["home"], rows, paths["workspace"])
    fake_lens(paths["home"], paths["workspace"])
    print(tr("데모 세계: {path}", path=root))
    aliases = [(str(paths["workspace"]), "~/code"), (str(paths["registry"]), "~/.harnist/registry"), (str(paths["home"]), "~/.claude"),
               (H.tilde(paths["workspace"]), "~/code"), (H.tilde(paths["registry"]), "~/.harnist/registry"), (H.tilde(paths["home"]), "~/.claude")]
    H.serve(paths["workspace"], port, [], open_browser, auto_baseline=False, demo=True, aliases=aliases)
