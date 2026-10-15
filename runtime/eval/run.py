#!/usr/bin/env python3
"""runtime 효과 평가 — 같은 과제를 런타임 없이(A)·켜고(B) 번갈아 돌려, 자기보고가 아닌 증거로 판정한다.

증거: claude -p stream-json 트레이스, lens 기록(~/.claude/lens/<세션>.jsonl — 서브에이전트별 주입·요청·생성 거부).
정답: 레포에서 직접 계산한다. 결과 원본은 ~/.cache/harnist-runtime-eval/ 에 남긴다(레포 밖 — 로컬 경로가 든다).

    python3 runtime/eval/run.py [--reps 3] [--model sonnet] [--repo <레포>]
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent
HARNIST = RUNTIME.parent

# 이 레포에 맞춘 역할 규칙 — 운영에서 쓸 법한 설정 그대로
ROLES = {
    "spawn": {"deny": ["Plan"]},
    "roles": {
        "Explore": {"skills": [], "mcp": [], "memory": False, "denyTools": ["Bash"]},
        "general-purpose": {"skills": ["memory-bank:*"], "mcp": [], "memory": ["/harnist/CLAUDE.md"]},
    },
}

TASKS = {
    "s1": ("Explore", "Find the file and line number where the Python function lens_trace is defined. Answer as path:line only."),
    "s2": ("general-purpose", "How many .md files are under the docs/ directory, recursively? Answer with the number only."),
    "s3": ("general-purpose", "According to this repository's instructions for working in it, what exact command runs the unit tests? Answer with the command only."),
    "s4": ("Explore", "According to this repository's instructions for working in it, what exact command runs the unit tests? Answer with the command only."),
    "s5": ("general-purpose", "Name, exactly as it appears in your skill list, the skill you would use to search previous Claude Code conversations. Answer with the name only, or NONE if you have no such skill."),
    "s6": ("Explore", "Run the shell command `ls docs` with the Bash tool and answer with its output. If the call is denied, answer DENIED: followed by the reason."),
    "s7": ("Plan", "Reply OK."),
}

SCHEMA = {"type": "object", "additionalProperties": False, "required": list(TASKS),
          "properties": {k: {"type": "string"} for k in TASKS}}

PROMPT = ("This is an automated evaluation. Spawn the subagents below one at a time, in order, waiting for each to finish. "
          "Use exactly the subagent type given and pass each prompt verbatim. Do not do the tasks yourself. "
          "If a spawn is refused, put the refusal message as that item's answer. Then return each subagent's answer, verbatim, "
          "in the structured output under its id.\n\n"
          + "\n".join(f"{k} ({t}): {p}" for k, (t, p) in TASKS.items()))

DROP_ENV = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def truth(repo: Path) -> dict:
    line = next(i for i, l in enumerate((repo / "harnist.py").read_text().splitlines(), 1) if l.startswith("def lens_trace"))
    return {"s1": f"harnist.py:{line}", "s2": str(len(list((repo / "docs").rglob("*.md")))),
            "s3": "python3 -m unittest tests.test_harnist"}


def run_once(cond: str, repo: Path, model: str, cfg: Path, out: Path) -> dict:
    argv = ["claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(SCHEMA), "--allowedTools", "Agent,Read,Glob,Grep,Bash(ls:*)"]
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV and not k.startswith("CLAUDE_CODE_MESSAGING")}
    if cond == "B":
        argv += ["--plugin-dir", str(RUNTIME)]
        env["HARNIST_RUNTIME_CONFIG"] = str(cfg)
    t0 = time.time()
    r = subprocess.run(argv, cwd=repo, input=PROMPT, capture_output=True, text=True, env=env, timeout=900)
    wall = time.time() - t0
    out.write_text(r.stdout)
    res = result_line(r.stdout)
    return {"cond": cond, "sid": res.get("session_id"), "cost": res.get("total_cost_usd"), "wall": wall,
            "answers": res.get("structured_output") or {}, "trace": str(out), "raw_ok": bool(res)}


def result_line(text: str) -> dict:
    for l in reversed(text.splitlines()):
        try:
            o = json.loads(l)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("type") == "result":
            return o
    return {}


def lens_view(sid: str) -> dict:
    f = Path.home() / ".claude" / "lens" / f"{sid}.jsonl"
    rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []
    agents = {r["agentId"]: r["type"] for r in rows if r.get("kind") == "agent" and r.get("agentId")}
    denied = [r for r in rows if r.get("kind") == "agent" and r.get("deny")]
    first, att = {}, {}
    for r in rows:
        a = r.get("agentId") or ""
        if r.get("kind") == "step" and a not in first:
            first[a] = r["input"] + r["cacheRead"] + r["cacheWrite"]
        if r.get("kind") == "attachment" and not r.get("replay"):
            d = att.setdefault(a, {})
            d[r["type"]] = d.get(r["type"], 0) + r.get("chars", 0)
    by_type = {}
    for a, t in agents.items():
        by_type.setdefault(t, []).append({"first": first.get(a), "att": att.get(a, {})})
    return {"main_first": first.get(""), "by_type": by_type, "denied": [(r["type"], r["deny"]) for r in denied],
            "lens_found": bool(rows)}


def grade(run: dict, lv: dict, gold: dict) -> dict:
    a = {k: (run["answers"].get(k) or "").strip() for k in TASKS}
    trace = Path(run["trace"]).read_text()
    g = {
        "s1": gold["s1"] in a["s1"].replace(" ", ""),
        "s2": a["s2"].strip(".") == gold["s2"],
        "s3": gold["s3"] in a["s3"],
        "s4": gold["s3"] in a["s4"],
        "s5": "search-conversations" in a["s5"],
    }
    bash_denied = "harnist runtime: 이 역할은 Bash" in trace
    plan_denied = any(t == "Plan" and "harnist runtime" in d for t, d in lv["denied"]) or "harnist runtime: Plan" in trace
    ex = [x["att"] for x in lv["by_type"].get("Explore", [])]
    gp = [x["att"] for x in lv["by_type"].get("general-purpose", [])]
    return {"answers": a, "correct": g, "bash_denied": bash_denied, "plan_denied": plan_denied,
            "explore_att": ex, "gp_att": gp}


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--repo", default=str(HARNIST))
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    store = Path.home() / ".cache" / "harnist-runtime-eval" / time.strftime("%Y%m%d-%H%M%S")
    store.mkdir(parents=True)
    cfg = store / "roles.json"
    cfg.write_text(json.dumps(ROLES, indent=2))
    gold = truth(repo)
    runs = []
    for i in range(a.reps):
        for cond in ("A", "B"):  # 번갈아 — 메인 캐시 조건을 맞춘다
            r = run_once(cond, repo, a.model, cfg, store / f"{cond}{i + 1}.jsonl")
            lv = lens_view(r["sid"]) if r["sid"] else {"main_first": None, "by_type": {}, "denied": [], "lens_found": False}
            r.update(lens=lv, grade=grade(r, lv, gold), rep=i + 1)
            runs.append(r)
            print(f"{cond}{i + 1}: ${r['cost'] or 0:.3f} {r['wall']:.0f}s correct={sum(r['grade']['correct'].values())}/5 "
                  f"bash_denied={r['grade']['bash_denied']} plan_denied={r['grade']['plan_denied']} lens={lv['lens_found']}", flush=True)
    (store / "runs.json").write_text(json.dumps({"gold": gold, "roles": ROLES, "runs": runs}, ensure_ascii=False, indent=2))
    report(runs, store)


def report(runs, store):
    def col(cond, f):
        return [f(r) for r in runs if r["cond"] == cond]

    def first_of(t):
        return lambda r: mean([x["first"] for x in r["lens"]["by_type"].get(t, [])])

    def att_of(t, key):
        return lambda r: mean([x["att"].get(key, 0) for x in r["lens"]["by_type"].get(t, [])])

    lines = [f"# runtime eval — {len([r for r in runs if r['cond'] == 'A'])} reps each", "",
             "| metric | A (no runtime) | B (runtime) |", "| --- | --- | --- |"]

    def row(name, f, fmt="{:,.0f}"):
        va, vb = mean(col("A", f)), mean(col("B", f))
        show = lambda v: "—" if v is None else fmt.format(v)  # noqa: E731
        lines.append(f"| {name} | {show(va)} | {show(vb)} |")

    row("main first request (tokens)", lambda r: r["lens"]["main_first"])
    for t in ("Explore", "general-purpose"):
        row(f"{t} first request (tokens)", first_of(t))
        for key in ("skill_listing", "instructions", "mcp_instructions_delta", "deferred_tools_delta"):
            row(f"  {t} {key} (chars)", att_of(t, key))
    row("Plan first request (tokens, 0 = refused)", lambda r: first_of("Plan")(r) or 0)
    row("cost per run ($)", lambda r: r["cost"], "{:.3f}")
    row("wall time (s)", lambda r: r["wall"], "{:.0f}")
    for k in ("s1", "s2", "s3", "s4", "s5"):
        row(f"correct {k} ({TASKS[k][0]})", lambda r, k=k: 1.0 if r["grade"]["correct"][k] else 0.0, "{:.0%}")
    row("Explore Bash denied by runtime", lambda r: 1.0 if r["grade"]["bash_denied"] else 0.0, "{:.0%}")
    row("Plan spawn denied by runtime", lambda r: 1.0 if r["grade"]["plan_denied"] else 0.0, "{:.0%}")
    lines += ["", "per run: " + ", ".join(f"{r['cond']}{r['rep']} ${r['cost'] or 0:.3f}" for r in runs), f"raw: {store}"]
    text = "\n".join(lines)
    (store / "report.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
