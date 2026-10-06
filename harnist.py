#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6"]
# ///
"""harnist — Tuist식 Claude Code 하네스 매니페스트 생성기.

레포에는 harness.yaml(무엇에 의존하는가)만 두고, .claude/ 아래 skills·agents·
settings.json·.mcp.json 과 CLAUDE.md 의 관리 블록은 레지스트리 모듈을 합성해 생성한다.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VERSION = 1
LAYERS = {"base": 0, "domain": 1, "project": 2}
BEGIN = "<!-- harnist:begin -->"
END = "<!-- harnist:end -->"
BLOCK_KEY = "CLAUDE.md#harnist"


# ---------------------------------------------------------------- i18n
# 사용자에게 보이는 문구는 한국어 원문을 키로 tr() 에 넘긴다. 기본은 영어(아래 EN 사전), 로케일이 ko 면 원문.


def ui_lang() -> str:
    """HARNIST_LANG → LC_ALL → LC_MESSAGES → LANG 중 처음 값이 있는 것이 ko 로 시작하면 "ko", 아니면 "en". 매번 읽는다."""
    for var in ("HARNIST_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var)
        if v:
            return "ko" if v.lower().startswith("ko") else "en"
    if sys.platform == "win32":
        import locale
        try:
            loc = locale.getlocale()[0] or ""
        except ValueError:
            loc = ""
        if loc.lower().startswith("ko"):
            return "ko"
    return "en"


def tr(ko: str, **kw) -> str:
    """ko = 한국어 템플릿. 영어 로케일이면 EN 의 번역을, 없으면 원문을 쓴다. 자리표시자는 같다."""
    return (ko if ui_lang() == "ko" else EN.get(ko, ko)).format(**kw)


# usage_report 의 verdict 값은 웹 UI·recommendations 가 읽는 데이터 키다 — 값은 그대로 두고 CLI 표시만 바꾼다.
VERDICT_EN = {"미사용": "Unused", "제한": "Narrow", "호출 기록 없음": "No calls", "공통": "Common",
              "보관됨": "Archived", "상시 실행": "Always runs"}


def verdict_label(v: str) -> str:
    return v if ui_lang() == "ko" else VERDICT_EN.get(v, v)


def block_key(fname: str) -> str:
    return f"{fname}#harnist"


def is_block_key(rel: str) -> bool:
    return rel.endswith("#harnist")
SKIP_NAMES = {".DS_Store", "__pycache__", ".venv", "node_modules"}
HERE = Path(__file__).resolve().parent


def _user_dir(name: str) -> Path:
    """개인 데이터(레지스트리·글로벌 매니페스트) 위치. 레포 안에 있으면 그것(개발용, gitignore), 없으면 ~/.harnist/."""
    local = HERE / name
    return local if local.exists() else Path(os.environ.get("HARNIST_DATA", "~/.harnist")).expanduser() / name


DEFAULT_REGISTRY = Path(os.environ["HARNIST_REGISTRY_HOME"]).expanduser() if os.environ.get("HARNIST_REGISTRY_HOME") else _user_dir("registry")


class HarnistError(Exception):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expand(p: str, base: Path) -> Path:
    q = Path(os.path.expandvars(os.path.expanduser(str(p))))
    return q if q.is_absolute() else (base / q).resolve()


def load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raise HarnistError(tr("파일 없음: {path}", path=path))


# ---------------------------------------------------------------- registry


@dataclass
class Module:
    name: str
    dir: Path
    spec: dict

    @property
    def layer(self) -> str:
        return self.spec.get("layer", "domain")

    @property
    def requires(self) -> list[str]:
        return self.spec.get("requires", [])

    @property
    def source(self) -> Path:
        s = self.spec.get("source")
        return expand(s, self.dir) if s else self.dir


def shared_registries(man: dict, base: Path) -> list[Path]:
    dirs = [Path(p).expanduser() for p in os.environ.get("HARNIST_REGISTRY", "").split(os.pathsep) if p]
    dirs += [expand(p, base) for p in man.get("registries", [])]
    return dirs or [DEFAULT_REGISTRY]


def local_registry(base: Path) -> Path:
    """프로젝트 전용(project 계층) 모듈 — 레포 안에 두고, 쓸모가 증명되면 promote 로 공유 레지스트리에 올린다."""
    return base / ".harnist" / "modules"


def load_registry(dirs: list[Path], local: Path | None = None) -> dict[str, Module]:
    mods: dict[str, Module] = {}
    if DEFAULT_REGISTRY in dirs and not DEFAULT_REGISTRY.exists():
        DEFAULT_REGISTRY.mkdir(parents=True, exist_ok=True)  # 처음 쓰는 사람 — 빈 레지스트리로 시작
    if local is not None and local.is_dir():
        dirs = [*dirs, local]
    for root in dirs:
        if not root.is_dir():
            raise HarnistError(tr("레지스트리 디렉터리 없음: {path}", path=root))
        for f in sorted(root.rglob("module.yaml")):
            spec = load_yaml(f)
            name = f.parent.relative_to(root).as_posix()
            if spec.get("name", name) != name:
                raise HarnistError(tr("{file}: name '{name}' 이 경로 '{path}' 과 다름", file=f, name=spec.get("name"), path=name))
            if spec.get("layer", "domain") not in LAYERS:
                raise HarnistError(tr("{name}: 알 수 없는 layer '{layer}'", name=name, layer=spec.get("layer")))
            if (root == local) != (spec.get("layer") == "project"):
                raise HarnistError(tr("{name}: project 계층 모듈은 레포의 .harnist/modules 에만, 그 밖의 계층은 공유 레지스트리에만 둔다", name=name))
            if name in mods:
                raise HarnistError(tr("모듈 중복: {name} ({a} / {b})", name=name, a=mods[name].dir, b=f.parent))
            mods[name] = Module(name, f.parent, spec)
    return mods


def resolve(roots: list[str], reg: dict[str, Module]) -> list[Module]:
    """의존 순서(의존 대상 먼저)로 정렬. 순환·누락·계층 역전은 오류."""
    order: list[Module] = []
    state: dict[str, int] = {}

    def visit(name: str, chain: list[str]) -> None:
        if name not in reg:
            raise HarnistError(tr("모듈 없음: {name} (경로: {path})", name=name, path=" → ".join(chain)))
        if state.get(name) == 1:
            raise HarnistError(tr("순환 의존: {path}", path=" → ".join(chain + [name])))
        if state.get(name) == 2:
            return
        state[name] = 1
        m = reg[name]
        for dep in m.requires:
            if dep in reg and LAYERS[reg[dep].layer] > LAYERS[m.layer]:
                raise HarnistError(tr(
                    "계층 역전: {name}({layer}) 이 {dep}({dep_layer}) 에 의존 — 하위 계층은 상위 계층을 알 수 없다",
                    name=name, layer=m.layer, dep=dep, dep_layer=reg[dep].layer))
            visit(dep, chain + [name])
        state[name] = 2
        order.append(m)

    for n in roots:
        visit(n, ["harness.yaml"])
    return order


# ---------------------------------------------------------------- plan


@dataclass
class Plan:
    root: Path  # 출력 루트 (project: 레포 루트, user: ~/.claude 에 해당하는 디렉터리)
    scope: str
    manifest_path: Path
    modules: list[Module]
    files: dict[str, bytes] = field(default_factory=dict)
    block: str | None = None
    mirrors: list = field(default_factory=list)  # 같은 규칙 블록을 받을 다른 에이전트용 파일 (예: AGENTS.md)
    links: str = "refuse"  # 출력 경로가 심볼릭 링크를 지날 때: refuse | skip | follow
    skipped: list = field(default_factory=list)
    lock: dict = field(default_factory=dict)

    @property
    def blocks(self) -> dict[str, str]:
        if self.block is None:
            return {}
        return {f: self.block for f in ["CLAUDE.md", *self.mirrors]}

    @property
    def claude_dir(self) -> str:
        return ".claude/" if self.scope == "project" else ""

    @property
    def ledger_path(self) -> Path:
        return self.root / self.claude_dir / ".harnist" / "ledger.json"


def deep_merge(dst: dict, src: dict, origin: str, owners: dict, path: str = "", override: bool = False) -> None:
    for k, v in src.items():
        p = f"{path}.{k}" if path else k
        if k not in dst:
            dst[k] = copy.deepcopy(v)
            owners[p] = origin
        elif isinstance(dst[k], dict) and isinstance(v, dict):
            deep_merge(dst[k], v, origin, owners, p, override)
        elif isinstance(dst[k], list) and isinstance(v, list):
            dst[k] += [x for x in v if x not in dst[k]]
        elif dst[k] != v:
            if not override:
                raise HarnistError(tr("settings 충돌 {key}: {a} 와 {b} 의 값이 다름", key=p, a=owners.get(p, "?"), b=origin))
            dst[k] = copy.deepcopy(v)


def iter_files(d: Path):
    for f in sorted(d.rglob("*")):
        rel = f.relative_to(d)
        if f.is_file() and not (set(rel.parts) & SKIP_NAMES) and f.suffix != ".pyc":
            yield f, rel.as_posix()


def apply_rewrites(rel: str, data: bytes, rules: list[dict], vars_: dict[str, str]) -> bytes:
    hit = [r for r in rules if any(fnmatch.fnmatch(Path(rel).name, g) for g in r.get("files", ["*.md"]))]
    if not hit:
        return data
    text = data.decode()
    for r in hit:
        repl = r["replace"]
        for k, v in vars_.items():
            repl = repl.replace("${" + k + "}", v.replace("\\", "\\\\"))
        text = re.sub(r["pattern"], repl, text)
    return text.encode()


def set_model(text: str, model: str, where: str) -> str:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise HarnistError(tr("{where}: frontmatter 가 없어 model 을 지정할 수 없음", where=where))
    lines = m.group(1).split("\n")
    idx = [i for i, l in enumerate(lines) if l.startswith("model:")]
    if idx:
        lines[idx[0]] = f"model: {model}"
    else:
        lines.append(f"model: {model}")
    return "---\n" + "\n".join(lines) + "\n---\n" + text[m.end():]


def marketplace_commit(name: str) -> str | None:
    home = Path(os.environ.get("HARNIST_PLUGIN_HOME", "~/.claude/plugins/marketplaces")).expanduser()
    d = home / name
    if not (d / ".git").exists():
        return None
    r = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or None


def build_plan(manifest_path: Path, out: Path | None = None) -> Plan:
    manifest_path = manifest_path.resolve()
    man = load_yaml(manifest_path)
    base = manifest_path.parent
    scope = man.get("scope", "project")
    if scope not in ("project", "user"):
        raise HarnistError(tr("알 수 없는 scope: {scope}", scope=scope))
    if out is None and man.get("target"):
        out = expand(man["target"], base)
    if scope == "user" and out is None:
        raise HarnistError(tr("user 스코프는 매니페스트 target 이나 --out 으로 출력 위치를 명시해야 한다"))

    reg = load_registry(shared_registries(man, base), local_registry(base))
    mods = resolve(man.get("modules", []), reg)

    root = (out or base).resolve()
    plan = Plan(root, scope, manifest_path, mods)
    cd = plan.claude_dir
    # project 스코프는 CWD=레포 루트이므로 상대 경로 유지(클론 위치 무관), user 스코프는 ~ 표기
    if scope == "project":
        install = ".claude"
    else:
        home = str(Path.home())
        install = "~" + str(root)[len(home):] if str(root).startswith(home + "/") else str(root)
    providers: dict[str, str] = {}
    settings: dict = {}
    owners: dict = {}
    mcp: dict = {}
    agents: dict[str, str] = {}  # agent name -> rel path
    skills: set[str] = set()
    fragments: list[tuple[Module, str]] = []
    lock_mods: dict = {}

    def emit(rel: str, data: bytes, origin: str) -> None:
        if rel in providers:
            raise HarnistError(tr("출력 충돌: {path} 을 {a} 와 {b} 가 동시에 제공", path=rel, a=providers[rel], b=origin))
        providers[rel] = origin
        plan.files[rel] = data

    for m in mods:
        spec, src = m.spec, m.source
        h = hashlib.sha256((m.dir / "module.yaml").read_bytes())
        vars_ = {"install": install, "source": str(src), "source_repo": str(src.parent)}
        rules = spec.get("rewrites", [])

        def take(path: Path, rel_in: str, rel_out: str) -> None:
            data = path.read_bytes()
            h.update(rel_in.encode() + b"\0" + data)
            emit(rel_out, apply_rewrites(rel_in, data, rules, vars_), m.name)

        for s in spec.get("skills", []):
            d = src / "skills" / s
            if not (d / "SKILL.md").is_file():
                raise HarnistError(tr("{module}: 스킬 없음 {path}/SKILL.md", module=m.name, path=d))
            for f, rel in iter_files(d):
                take(f, f"skills/{s}/{rel}", f"{cd}skills/{s}/{rel}")
            skills.add(s)
        for a in spec.get("agents", []):
            f = src / "agents" / f"{a}.md"
            if not f.is_file():
                raise HarnistError(tr("{module}: 에이전트 없음 {path}", module=m.name, path=f))
            take(f, f"agents/{a}.md", f"{cd}agents/{a}.md")
            agents[a] = f"{cd}agents/{a}.md"
        for p in spec.get("plugins", []):
            pid, mk = p["id"], p["id"].split("@")[-1]
            frag = {"enabledPlugins": {pid: True}}
            if "marketplace" in p:
                frag["extraKnownMarketplaces"] = {mk: {"source": p["marketplace"]}}
            deep_merge(settings, frag, m.name, owners)
            lock_mods.setdefault("@marketplaces", {})[mk] = marketplace_commit(mk)
        for name, cfg in (spec.get("mcp") or {}).items():
            if name in mcp and mcp[name] != cfg:
                raise HarnistError(tr("MCP 충돌: {name} 을 여러 모듈이 다르게 정의", name=name))
            mcp[name] = cfg
        if spec.get("settings"):
            deep_merge(settings, spec["settings"], m.name, owners)
        if spec.get("claude_md"):
            fragments.append((m, spec["claude_md"].strip()))
        lock_mods[m.name] = {"layer": m.layer, "hash": h.hexdigest()}

    # 프로젝트 계층: spawn·teams·overrides
    spawn = man.get("spawn") or {}
    for a, model in (spawn.get("routing") or {}).items():
        if a not in agents:
            raise HarnistError(tr("spawn.routing: 에이전트 '{agent}' 를 제공하는 모듈이 없음", agent=a))
        rel = agents[a]
        plan.files[rel] = set_model(plan.files[rel].decode(), model, rel).encode()
    teams = man.get("teams") or {}
    for t, cfg in teams.items():
        for mem in cfg.get("members", []):
            if mem not in agents and mem not in skills:
                raise HarnistError(tr("teams.{team}: 멤버 '{member}' 가 제공된 에이전트·스킬에 없음", team=t, member=mem))
    ov = man.get("overrides") or {}
    if ov.get("settings"):
        deep_merge(settings, ov["settings"], "overrides", owners, override=True)
    mcp.update(ov.get("mcp") or {})

    if scope == "user" and (settings or mcp):
        raise HarnistError(tr(
            "user 스코프는 skills·agents·CLAUDE.md 블록만 관리한다 — 플러그인·settings·MCP 를 쓰는 모듈은 project 매니페스트에서 사용"))
    if settings:
        plan.files[f"{cd}settings.json"] = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode()
    if mcp:
        plan.files[".mcp.json"] = (json.dumps({"mcpServers": mcp}, ensure_ascii=False, indent=2) + "\n").encode()

    local_md = expand(ov["claude_md"], base).read_text().strip() if ov.get("claude_md") else ""
    plan.block = render_block(mods, fragments, spawn, teams, local_md)
    mirrors = man.get("mirror") or []
    for f in mirrors:
        if "/" in f or not f.endswith(".md") or f == "CLAUDE.md":
            raise HarnistError(tr("mirror 는 레포 루트의 .md 파일 이름이어야 함: {file}", file=f))
    if mirrors and scope == "user":
        raise HarnistError(tr("user 스코프는 mirror 를 지원하지 않는다 (각 에이전트의 전역 위치가 다름)"))
    plan.mirrors = list(mirrors)
    plan.links = man.get("links", "refuse")
    if plan.links not in ("refuse", "skip", "follow"):
        raise HarnistError(tr("links 는 refuse | skip | follow 중 하나: {value}", value=plan.links))
    if plan.links == "skip":
        plan.skipped = sorted(r for r in plan.files if through_link(root, r))
        for r in plan.skipped:
            del plan.files[r]
    plan.lock = {"version": VERSION, "modules": lock_mods}
    return plan


def render_block(mods, fragments, spawn, teams, local_md) -> str:
    out = [
        BEGIN,
        tr("<!-- 생성물: harness.yaml 을 고치고 `harnist generate` 로 재생성한다. 직접 고치면 check 가 드리프트로 잡는다. -->"),
        tr("## 하네스 모듈"),
        "",
        tr("| 모듈 | 계층 | 제공 |"),
        "| --- | --- | --- |",
    ]
    for m in mods:
        s = m.spec
        parts = [f"{k} {len(s[k])}" for k in ("skills", "agents", "plugins") if s.get(k)]
        if s.get("mcp"):
            parts.append(f"mcp {len(s['mcp'])}")
        provides = ", ".join(parts) or tr("규칙만")
        out.append(f"| {m.name} | {m.layer} | {provides} |")
    for m, frag in fragments:
        out += ["", f"### {m.name}", "", frag]
    if teams:
        out += ["", tr("### 팀")]
        for t, cfg in teams.items():
            lead = tr(" · 리드 `{lead}`", lead=cfg["lead"]) if cfg.get("lead") else ""
            out.append("\n" + tr("**{team}** — {purpose}  \n구성: {members}{lead}", team=t, purpose=cfg.get("purpose", ""),
                                  members=", ".join(cfg.get("members", [])), lead=lead))
    if spawn.get("max_parallel_agents") or spawn.get("rules") or spawn.get("routing"):
        out += ["", tr("### 스폰 규칙"), ""]
        if spawn.get("max_parallel_agents"):
            out.append(tr("한 번에 동시에 띄우는 서브에이전트는 {n}개를 넘기지 않는다.", n=spawn["max_parallel_agents"]))
        if spawn.get("routing"):
            out.append(tr("모델 라우팅(에이전트 frontmatter 에 반영됨): {routes}",
                          routes=", ".join(f"{a}→{m}" for a, m in spawn["routing"].items())))
        if spawn.get("rules"):
            out.append("")
            out += [f"- {r}" for r in spawn["rules"]]
    if local_md:
        out += ["", tr("### 이 레포 고유"), "", local_md]
    out.append(END)
    return "\n".join(out)


# ---------------------------------------------------------------- apply


def through_link(root: Path, rel: str) -> bool:
    """root 아래 rel 경로의 어느 구간이든 심볼릭 링크면 True — 링크 너머는 harnist 가 만든 것이 아니다."""
    p = root
    for part in Path(rel).parts:
        p = p / part
        if p.is_symlink():
            return True
    return False


def read_ledger(plan: Plan) -> dict:
    try:
        return json.loads(plan.ledger_path.read_text())["files"]
    except FileNotFoundError:
        return {}


def ledger_manifest(plan: Plan) -> str | None:
    try:
        return json.loads(plan.ledger_path.read_text()).get("manifest")
    except FileNotFoundError:
        return None


def current_block(text: str) -> str | None:
    ends = [m.start() for m in re.finditer(rf"(?m)^{re.escape(END)}[ \t]*$", text)]
    if not ends:
        return None
    begins = [m.start() for m in re.finditer(rf"(?m)^{re.escape(BEGIN)}[ \t]*$", text) if m.start() < ends[0]]
    return text[begins[-1] : ends[0] + len(END)] if begins else None


def compute_actions(plan: Plan, ledger: dict, adopt: bool = False, force: bool = False):
    """(actions, conflicts). action = (kind, rel) — kind ∈ create/update/delete/adopt."""
    actions, conflicts = [], []
    owner = ledger_manifest(plan)
    if owner and Path(owner) != plan.manifest_path and not force:
        return [], [tr("이 출력 폴더는 다른 매니페스트가 관리한다: {path} (--force 로 넘겨받기)", path=tilde(owner))]
    for rel, data in plan.files.items():
        if plan.links != "follow" and through_link(plan.root, rel):
            conflicts.append(tr("심볼릭 링크 너머 경로: {path} — 다른 도구가 관리하는 곳 (매니페스트 links: skip 으로 건너뛰기)", path=rel))
            continue
        p = plan.root / rel
        if not p.exists():
            actions.append(("create", rel))
            continue
        cur = sha(p.read_bytes())
        if cur == sha(data):
            if rel not in ledger:
                actions.append(("adopt", rel))
        elif rel in ledger and (cur == ledger[rel] or force):
            actions.append(("update", rel))
        elif rel in ledger:
            conflicts.append(tr("수동 수정됨: {path} (--force 로 덮어쓰기)", path=rel))
        elif adopt:
            actions.append(("update", rel))
        else:
            conflicts.append(tr("관리 밖 파일: {path} (--adopt 로 편입)", path=rel))
    for rel, h in ledger.items():
        if is_block_key(rel) or rel in plan.files:
            continue
        if through_link(plan.root, rel):
            continue  # 장부에서만 뺀다 — 링크 너머 파일은 지우지 않는다
        p = plan.root / rel
        if p.exists():
            if sha(p.read_bytes()) == h or force:
                actions.append(("delete", rel))
            else:
                conflicts.append(tr("수동 수정된 파일이 더 이상 생성되지 않음: {path} (--force 로 삭제)", path=rel))
    blocks = plan.blocks
    for fname, block in blocks.items():
        key, md = block_key(fname), plan.root / fname
        cur = current_block(md.read_text()) if md.exists() else None
        if cur == block:
            if key not in ledger:
                actions.append(("adopt", key))
        elif cur is None:
            actions.append(("create", key))
        elif key in ledger and (sha(cur.encode()) == ledger[key] or force):
            actions.append(("update", key))
        elif key in ledger:
            conflicts.append(tr("수동 수정됨: {file} 의 harnist 블록 (--force 로 덮어쓰기)", file=fname))
        elif adopt:
            actions.append(("update", key))
        else:
            conflicts.append(tr("관리 밖 harnist 블록: {file} (--adopt 로 편입)", file=fname))
    for key, h in ledger.items():
        fname = key[: -len("#harnist")]
        if not is_block_key(key) or fname in blocks:
            continue
        md = plan.root / fname
        cur = current_block(md.read_text()) if md.exists() else None
        if cur is None:
            continue
        if sha(cur.encode()) == h or force:
            actions.append(("delete", key))
        else:
            conflicts.append(tr("수동 수정된 harnist 블록이 더 이상 생성되지 않음: {file} (--force 로 제거)", file=fname))
    return actions, conflicts


def lock_path(plan: Plan) -> Path:
    return plan.manifest_path.with_name("harness.lock")


def lock_diff(plan: Plan) -> list[str]:
    try:
        old = json.loads(lock_path(plan).read_text())["modules"]
    except FileNotFoundError:
        return [tr("harness.lock 없음")]
    new = plan.lock["modules"]
    diffs = []
    for k in sorted(set(old) | set(new)):
        if k not in old:
            diffs.append(f"+ {k}")
        elif k not in new:
            diffs.append(f"- {k}")
        elif old[k] != new[k]:
            if k == "@marketplaces":
                for mk in sorted(set(old[k]) | set(new[k])):
                    if old[k].get(mk) != new[k].get(mk):
                        diffs.append(tr("~ 마켓플레이스 {name}: {a} → {b}", name=mk, a=str(old[k].get(mk))[:7], b=str(new[k].get(mk))[:7]))
            else:
                diffs.append(tr("~ {name} (내용 변경)", name=k))
    return diffs


def write_lock(plan: Plan) -> None:
    lock_path(plan).write_text(json.dumps(plan.lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def apply(plan: Plan, actions, ledger: dict) -> None:
    for kind, rel in actions:
        if is_block_key(rel):
            fname = rel[: -len("#harnist")]
            md = plan.root / fname
            text = md.read_text() if md.exists() else ""
            cur = current_block(text)
            if kind == "delete":
                rest = text.replace(cur + "\n", "", 1) if cur + "\n" in text else text.replace(cur, "", 1)
                if rest.strip():
                    md.write_text(rest.rstrip() + "\n")
                else:
                    md.unlink()
                ledger.pop(rel, None)
                continue
            block = plan.blocks[fname]
            if kind != "adopt":
                if cur is not None:
                    text = text.replace(cur, block, 1)
                else:
                    text = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
                md.parent.mkdir(parents=True, exist_ok=True)
                md.write_text(text)
            ledger[rel] = sha(block.encode())
            continue
        p = plan.root / rel
        if kind == "delete":
            p.unlink()
            ledger.pop(rel, None)
            d = p.parent
            while d != plan.root and not d.is_symlink() and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
            continue
        if kind != "adopt":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(plan.files[rel])
        ledger[rel] = sha(plan.files[rel])
    for rel in [r for r in ledger if not is_block_key(r) and r not in plan.files]:
        ledger.pop(rel)
    plan.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    plan.ledger_path.write_text(
        json.dumps({"version": VERSION, "manifest": str(plan.manifest_path), "files": dict(sorted(ledger.items()))},
                   ensure_ascii=False, indent=2) + "\n"
    )


# ---------------------------------------------------------------- graph


def graph(plan: Plan, fmt: str) -> str:
    man = load_yaml(plan.manifest_path)
    if fmt == "json":
        return json.dumps({
            "manifest": str(plan.manifest_path),
            "scope": plan.scope,
            "roots": man.get("modules", []),
            "modules": [
                {"name": m.name, "layer": m.layer, "requires": m.requires,
                 **{k: m.spec.get(k, []) for k in ("skills", "agents")},
                 "plugins": [p["id"] for p in m.spec.get("plugins", [])],
                 "mcp": list((m.spec.get("mcp") or {}).keys())}
                for m in plan.modules
            ],
            "spawn": man.get("spawn") or {},
            "teams": man.get("teams") or {},
        }, ensure_ascii=False, indent=2)
    nid = lambda n: re.sub(r"\W", "_", n)
    lines = ["flowchart TB", f'  manifest["{plan.manifest_path.parent.name}/harness.yaml"]']
    for layer in ("project", "domain", "base"):
        ms = [m for m in plan.modules if m.layer == layer]
        if ms:
            lines.append(f"  subgraph {layer}")
            for m in ms:
                s = m.spec
                detail = " · ".join(f"{k} {len(s[k])}" for k in ("skills", "agents", "plugins", "mcp") if s.get(k))
                lines.append(f'    {nid(m.name)}["{m.name}<br/><small>{detail}</small>"]')
            lines.append("  end")
    for r in man.get("modules", []):
        lines.append(f"  manifest --> {nid(r)}")
    for m in plan.modules:
        for d in m.requires:
            lines.append(f"  {nid(m.name)} --> {nid(d)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- init / promote


def init_manifest(d: Path, modules: list[str]) -> Path:
    d = d.resolve()
    mp = d / "harness.yaml"
    if mp.exists():
        raise HarnistError(tr("이미 있음: {path}", path=mp))
    reg = load_registry(shared_registries({}, d), local_registry(d))
    missing = [m for m in modules if m not in reg]
    if missing:
        raise HarnistError(tr("레지스트리에 없는 모듈: {names}", names=", ".join(missing)))
    body = "".join(f"  - {m}\n" for m in modules)
    mp.write_text(
        tr("# harnist 매니페스트 — 이 레포가 쓰는 하네스 모듈 선언. .claude/ 는 `harnist generate` 의 생성물이다.") + "\n"
        + (f"modules:\n{body}" if body else "modules: []\n")
    )
    return mp


def promote(mp: Path, name: str, to: str) -> Path:
    """project 모듈을 공유 레지스트리의 base/domain 모듈로 올린다."""
    mp = mp.resolve()
    base, man = mp.parent, load_yaml(mp)
    local = local_registry(base)
    shared = shared_registries(man, base)
    reg = load_registry(shared, local)
    if name not in reg or reg[name].layer != "project":
        raise HarnistError(tr("{name}: 이 레포의 project 모듈이 아님", name=name))
    layer = to.split("/")[0]
    if layer not in ("base", "domain"):
        raise HarnistError(tr("승격 대상은 base/… 또는 domain/… 이어야 함: {to}", to=to))
    if to in reg:
        raise HarnistError(tr("이미 있는 모듈 이름: {name}", name=to))
    m = reg[name]
    local_deps = [d for d in m.requires if reg[d].layer == "project"]
    if local_deps:
        raise HarnistError(tr("project 모듈에 의존하고 있어 먼저 승격해야 함: {names}", names=", ".join(local_deps)))
    dest = shared[0] / to
    shutil.copytree(m.dir, dest)
    spec = dict(m.spec, name=to, layer=layer)
    if "source" in spec:
        spec["source"] = str(m.source)
    if (spec.get("skills") or spec.get("agents")) and not any("${install}" in r.get("replace", "") for r in spec.get("rewrites", [])):
        spec.setdefault("rewrites", []).append(
            {"pattern": r"(?<![\w~/.-])\.claude/(skills|agents)/", "replace": r"${install}/\1/"}
        )
    (dest / "module.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False))
    pat = re.compile(rf"(?<![\w/-]){re.escape(name)}(?![\w/-])")
    for f in [mp, *[x for x in local.rglob("module.yaml") if x.parent != m.dir]]:
        f.write_text(pat.sub(to, f.read_text()))
    shutil.rmtree(m.dir)
    d = m.dir.parent
    while d != local and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent
    return dest


# ---------------------------------------------------------------- usage / demote / attach

def claude_home() -> Path:
    return Path(os.environ.get("HARNIST_CLAUDE_HOME", "~/.claude")).expanduser()


def claude_json() -> dict:
    """사용자 스코프 MCP 가 들어 있는 ~/.claude.json (홈을 바꾸면 그 옆 파일)."""
    p = claude_home().parent / ".claude.json"
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        return {}
GLOBAL_MANIFEST = _user_dir("manifests") / "global" / "harness.yaml"


def frontmatter(path: Path) -> dict:
    m = re.match(r"---\n(.*?)\n---\n", path.read_text(errors="ignore"), re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        d = re.search(r"(?m)^description:[ \t]*(.*)$", m.group(1))
        return {"description": d.group(1).strip()} if d else {}


def global_items() -> list[dict]:
    """~/.claude 에 전역으로 붙어 있는 스킬·에이전트·플러그인. managed = harnist 글로벌 매니페스트가 만든 것."""
    try:
        managed = json.loads((claude_home() / ".harnist" / "ledger.json").read_text())["files"]
    except FileNotFoundError:
        managed = {}
    owner = {}
    try:
        gmods = build_plan(GLOBAL_MANIFEST, claude_home()).modules if GLOBAL_MANIFEST.exists() else []
    except HarnistError:
        gmods = []  # 원본 레포가 없어도 점검은 계속한다
    if True:
        for m in gmods:
            for k in ("skills", "agents"):
                for n in m.spec.get(k, []):
                    owner[(k[:-1], n)] = m.name
    items = []
    for d in sorted((claude_home() / "skills").glob("*/SKILL.md")):
        n = d.parent.name
        fm = frontmatter(d)
        if fm.get("harnist-stub"):
            items.append({"kind": "skill", "name": n, "desc": "", "module": None, "stub": fm["harnist-stub"]})
            continue
        items.append({"kind": "skill", "name": n, "desc": "" if fm.get("disable-model-invocation") else str(fm.get("description", "")),
                      "module": owner.get(("skill", n)) if f"skills/{n}/SKILL.md" in managed else None})
    for f in sorted((claude_home() / "agents").glob("*.md")):
        n = f.stem
        items.append({"kind": "agent", "name": n, "desc": str(frontmatter(f).get("description", "")),
                      "module": owner.get(("agent", n)) if f"agents/{n}.md" in managed else None})
    settings = read_json(claude_home() / "settings.json")
    installed = read_json(claude_home() / "plugins" / "installed_plugins.json")
    installed = installed.get("plugins", installed)
    for pid, on in (settings.get("enabledPlugins") or {}).items():
        if not on:
            continue
        descs = []
        for inst in installed.get(pid, [])[:1]:
            root = Path(inst.get("installPath", ""))
            found = [f for f in root.rglob("SKILL.md") if not (set(f.relative_to(root).parts) & SKIP_NAMES)]
            for f in [*found, *root.glob("agents/*.md"), *root.glob("commands/*.md")]:
                fm = frontmatter(f)
                if not fm.get("disable-model-invocation"):
                    descs.append(str(fm.get("description", "")))
        hook_only = not descs and any(
            (Path(i.get("installPath", "")) / "hooks" / "hooks.json").exists() for i in installed.get(pid, [])[:1])
        items.append({"kind": "plugin", "name": pid, "desc": " ".join(descs), "module": None, "hook_only": hook_only})
    for name, cfg in (claude_json().get("mcpServers") or {}).items():
        items.append({"kind": "mcp", "name": name, "desc": "", "module": None,
                      "secret": bool(cfg.get("env") or cfg.get("headers"))})
    for event, groups in (settings.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                label = Path(cmd.split()[-1].strip('"')).name if cmd else h.get("type", "")
                items.append({"kind": "hook", "name": f"{event}:{label}", "desc": "", "module": None})
    return items


MARKS = (".git", "harness.yaml", ".claude", "CLAUDE.md")
import tempfile  # noqa: E402

TEMP_PREFIXES = tuple({str(Path(tempfile.gettempdir()).resolve()) + os.sep, "/private/tmp/", "/tmp/",
                       "/private/var/folders/", "/var/folders/"})  # 서브에이전트 스크래치 등 — OS 임시 폴더 포함


def _is_project(q: Path) -> bool:
    return any((q / m).exists() for m in MARKS)


def _is_container(q: Path, cache: dict) -> bool:
    """하위에 프로젝트가 3개 이상인 폴더(예: Projects/)는 레포가 아니라 묶음으로 본다."""
    key = ("container", str(q))
    if key not in cache:
        try:
            cache[key] = sum(1 for c in q.iterdir() if c.is_dir() and _is_project(c)) >= 3
        except OSError:
            cache[key] = False
    return cache[key]


def repo_of(cwd: str, cache: dict) -> str:
    """세션 cwd → 레포. .git/harness.yaml 을 레포 경계로 보고, 그 사이에 프로젝트 묶음 폴더(Projects/ 같은)가 있으면
    묶음 바로 아래 폴더를 레포로 본다. .claude·CLAUDE.md 만 있는 작업 폴더(docs/design, _workspace/x)는 레포로 치지 않는다."""
    if cwd not in cache:
        p = Path(cwd)
        if TEMP_PREFIXES and str(p).startswith(TEMP_PREFIXES):
            cache[cwd] = "(임시 폴더)"
            return cache[cwd]
        stop = {Path.home(), claude_home()}
        chain = []
        for q in [p, *p.parents]:
            if q in stop or q == q.parent:
                break
            chain.append(q)
        strong = next((i for i, q in enumerate(chain) if (q / ".git").exists() or (q / "harness.yaml").exists()), None)
        if strong is None:
            weak = [i for i, q in enumerate(chain) if _is_project(q)]
            strong = weak[-1] if weak else None
        root = chain[strong] if strong is not None else p
        if strong is not None:
            for i in range(strong, 0, -1):  # 레포 경계 → cwd 방향으로 내려가며 묶음 폴더를 찾는다
                if _is_container(chain[i], cache):
                    root = chain[i - 1]
                    break
        cache[cwd] = tilde(root)
    return cache[cwd]


def scan_usage(days: int) -> dict:
    """세션 기록(~/.claude/projects/*/*.jsonl)에서 스킬·에이전트 호출을 레포별로 센다."""
    import time
    cutoff = time.time() - days * 86400
    use: dict = {}
    cache: dict = {}
    cmd = re.compile(r"<command-name>/?([\w:.-]+)</command-name>")

    def hit(kind, name, cwd, ts, sid):
        u = use.setdefault((kind, name), {"sessions": set(), "repos": {}, "last": ""})
        u["sessions"].add(sid)
        r = repo_of(cwd, cache) if cwd else "?"
        u["repos"][r] = u["repos"].get(r, 0) + 1
        u["last"] = max(u["last"], ts or "")

    for f in (claude_home() / "projects").glob("*/*.jsonl"):
        if f.stat().st_mtime < cutoff:
            continue
        for line in f.open(errors="ignore"):
            if '"tool_use"' not in line and "<command-name>" not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            cwd, ts = d.get("cwd"), d.get("timestamp", "")
            if d.get("type") == "user":
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content or [] if isinstance(b, dict))
                for n in cmd.findall(text):
                    hit("skill", n, cwd, ts, f.stem)
                continue
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                inp = b.get("input") or {}
                if b.get("name") == "Skill" and inp.get("skill"):
                    hit("skill", inp["skill"].lstrip("/"), cwd, ts, f.stem)
                elif b.get("name") in ("Agent", "Task") and inp.get("subagent_type"):
                    hit("agent", inp["subagent_type"], cwd, ts, f.stem)
                elif str(b.get("name", "")).startswith("mcp__"):
                    hit("mcp", b["name"].split("__")[1], cwd, ts, f.stem)
    return use


def usage_report(days: int) -> list[dict]:
    use = scan_usage(days)
    rows = []
    for it in global_items():
        if it["kind"] == "hook":
            rows.append({**it, "sessions": 0, "repos": {}, "calls": 0, "last": "", "verdict": "상시 실행",
                         "always_chars": 0})
            continue
        stub = it.get("stub")
        if it["kind"] == "plugin":
            pname = it["name"].split("@")[0]
            parts = [u for (k, n), u in use.items()
                     if (k != "mcp" and n.startswith(pname + ":")) or (k == "mcp" and n.startswith(f"plugin_{pname}_"))]
        else:
            parts = [u for (k, n), u in use.items() if k == it["kind"] and n == it["name"]]
        repos: dict = {}
        for u in parts:
            for r, c in u["repos"].items():
                repos[r] = repos.get(r, 0) + c
        sessions = len(set().union(*[u["sessions"] for u in parts])) if parts else 0
        last = max([u["last"] for u in parts] or [""])[:10]
        if stub:
            verdict = "보관됨"
        elif not parts:
            verdict = ("상시 실행" if it.get("hook_only") else "호출 기록 없음") if it["kind"] == "plugin" else "미사용"
        elif len([r for r in repos if r not in ("?", "(임시 폴더)")]) <= 2:
            verdict = "제한"
        else:
            verdict = "공통"
        rows.append({**it, "sessions": sessions, "calls": sum(repos.values()),
                     "repos": dict(sorted(repos.items(), key=lambda x: -x[1])),
                     "last": last, "verdict": verdict, "always_chars": len(it["desc"])})
    return rows


def audit_path() -> Path:
    return claude_home() / ".harnist" / "audit.json"


def item_ids(items: list[dict]) -> list[str]:
    return sorted(f"{i['kind']}:{i['name']}" for i in items if i["kind"] != "hook")


def mark_audit() -> Path:
    import time
    p = audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M"), "items": item_ids(global_items())},
                            ensure_ascii=False, indent=1) + "\n")
    return p


def new_since_audit() -> list[str] | None:
    """마지막 점검 이후 새로 생긴 전역 항목. 점검한 적 없으면 None."""
    snap = read_json(audit_path())
    if not snap:
        return None
    return sorted(set(item_ids(global_items())) - set(snap.get("items", [])))


def project_audit(rows: list[dict], use: dict | None = None) -> list[dict]:
    """레포별: 실제로 쓴 전역 항목(연결 후보)과, 레포에 있는데 안 쓴 로컬 항목(정리 후보)."""
    use = use if use is not None else scan_usage(90)
    repos: dict[str, dict] = {}
    for r in rows:
        for repo, c in r["repos"].items():
            if repo in ("?", "(임시 폴더)"):
                continue
            repos.setdefault(repo, {"repo": repo, "used_global": [], "unused_local": []})["used_global"].append(
                {"id": f"{r['kind']}:{r['name']}", "calls": c, "verdict": r["verdict"]})
    loaded = len([r for r in rows if r["kind"] != "hook" and not r.get("stub")])
    for repo, rec in repos.items():
        d = Path(repo).expanduser()
        used = len({u["id"] for u in rec["used_global"]})
        rec["fit"] = {"used": used, "loaded": loaded, "ratio": round(used / loaded, 3) if loaded else None}
        local = [("skill", x.parent.name) for x in d.glob(".claude/skills/*/SKILL.md")]
        local += [("agent", x.stem) for x in d.glob(".claude/agents/*.md")]
        local += [("mcp", n) for n in (read_json(d / ".mcp.json").get("mcpServers") or {})]
        for k, n in local:
            u = use.get((k, n))
            if not u or repo not in u["repos"]:
                rec["unused_local"].append(f"{k}:{n}")
        rec["used_global"].sort(key=lambda x: -x["calls"])
        rec["harness"] = (d / "harness.yaml").exists()
    return sorted(repos.values(), key=lambda r: -sum(x["calls"] for x in r["used_global"]))


def _modules_span(text: str) -> tuple[int, int, str, bool]:
    """modules: 블록의 (시작, 끝, 항목 들여쓰기, 인라인 빈 리스트 여부)."""
    m = re.search(r"(?m)^modules:[ \t]*(\[\])?[ \t]*(#.*)?(\n|$)", text)
    if not m:
        raise HarnistError(tr("modules: 블록을 찾지 못해 자동 편집할 수 없음 — 직접 추가"))
    if m.group(1):
        return m.start(), m.end(), "  ", True
    end, indent = m.end(), None
    for line in text[m.end():].splitlines(keepends=True):
        item = re.match(r"([ \t]*)-[ \t]", line)
        if item and (indent is None or item.group(1) == indent):
            indent = item.group(1) if indent is None else indent
        elif not (line.strip() == "" or line.lstrip().startswith("#")):
            break
        end += len(line)
    return m.start(), end, indent if indent is not None else "  ", False


def _manifest_modules_edit(mp: Path, add: str | None = None, remove: str | None = None) -> bool:
    text = mp.read_text()
    mods = load_yaml(mp).get("modules") or []
    if add and add in mods or remove and remove not in mods:
        return False
    start, end, indent, inline = _modules_span(text)
    block = text[start:end]
    if remove:
        block = re.sub(rf"(?m)^{re.escape(indent)}-[ \t]*{re.escape(remove)}[ \t]*(#.*)?(\n|$)", "", block, count=1)
        if not re.search(r"(?m)^[ \t]*-[ \t]", block):
            block = "modules: []\n" + "".join(l for l in block.splitlines(keepends=True)[1:] if l.lstrip().startswith("#"))
        want = [x for x in mods if x != remove]
    else:
        if inline:
            block = f"modules:\n{indent}- {add}\n"
        else:
            body = block.rstrip("\n")
            trail = block[len(body):]
            block = body + f"\n{indent}- {add}" + (trail or "\n")
        want = [*mods, add]
    new = text[:start] + block + text[end:]
    try:
        got = (yaml.safe_load(new) or {}).get("modules") or []
    except yaml.YAMLError as e:
        raise HarnistError(tr("{path}: 자동 편집 결과가 YAML 이 아님 — 파일은 그대로 두었다 ({error})", path=mp, error=e))
    if got != want:
        raise HarnistError(tr("{path}: 자동 편집 결과가 예상과 다름 — 파일은 그대로 두었다", path=mp))
    tmp = mp.with_suffix(".yaml.harnist-tmp")
    tmp.write_text(new)
    os.replace(tmp, mp)
    return True


def attach(repo: Path, module: str) -> Path:
    """레포에 모듈을 연결한다. harness.yaml 이 없으면 만든다."""
    repo = repo.expanduser().resolve()
    mp = repo / "harness.yaml"
    if not mp.exists():
        return init_manifest(repo, [module])
    reg = load_registry(shared_registries(load_yaml(mp), repo), local_registry(repo))
    if module not in reg:
        raise HarnistError(tr("레지스트리에 없는 모듈: {names}", names=module))
    _manifest_modules_edit(mp, add=module)
    return mp


def detach(repo: Path, module: str) -> Path:
    mp = repo.expanduser().resolve() / "harness.yaml"
    if not _manifest_modules_edit(mp, remove=module):
        raise HarnistError(tr("{path}: {module} 이 직접 선언되어 있지 않음", path=mp, module=module))
    return mp


SECRET_FILES = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "credentials*", "*.p12")
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|pk_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|(?i:token|secret|api[_-]?key|password|passwd)[^\n]{0,24}?[\"'=: ][0-9A-Za-z_\-]{32,})"
)


def find_secrets(paths: list[Path]) -> list[str]:
    hits = []
    for root in paths:
        files = [root] if root.is_file() else [f for f, _ in iter_files(root)]
        for f in files:
            try:
                text = f.read_text(errors="ignore")
            except OSError:
                continue
            for m in SECRET_RE.finditer(text):
                hits.append(f"{tilde(f)}: {m.group(0)[:6]}…")
                break
    return hits


def write_stub(name: str, module: str, skill_dir: Path, desc: str, origin: str) -> Path:
    """전역에서 내린 스킬 자리에 남기는 스텁. 상시 컨텍스트 0, /이름 으로 부르면 보관본을 읽어 수행한다."""
    import time
    d = claude_home() / "skills" / name
    if d.is_symlink():  # 링크 너머(예: ~/.agents/skills)는 다른 에이전트의 원본 — 링크만 떼어 백업하고 그 자리에 쓴다
        trash = Path(__file__).resolve().parent / ".trash" / time.strftime("%Y%m%d-%H%M%S") / "skills"
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(trash / name))
    elif d.exists() and not frontmatter(d / "SKILL.md").get("harnist-stub"):
        raise HarnistError(tr("{path} 에 스텁이 아닌 스킬이 있어 덮어쓰지 않는다", path=tilde(d)))
    d.mkdir(parents=True, exist_ok=True)
    short = re.sub(r"\s+", " ", desc).strip()[:70]
    fm = yaml.safe_dump({"name": name, "description": tr("(보관됨) {desc}", desc=short), "disable-model-invocation": True,
                         "harnist-stub": module, "harnist-origin": origin}, allow_unicode=True, sort_keys=False)
    body = "\n".join([
        tr("# {name} — harnist 레지스트리에 보관된 스킬", name=name),
        "",
        tr("{date} 전역 점검에서 사용 기록이 적어 전역에서 내리고 `{module}` 모듈로 보관했다. 사용자가 직접 불렀으니 그대로 수행한다.",
           date=time.strftime("%Y-%m-%d"), module=module),
        "",
        tr("1. `{dir}/SKILL.md` 를 읽고 그 절차를 따른다. 이 스킬의 기준 디렉터리는 `{dir}/` 이다. 본문에 `~/.claude/skills/{name}/` 경로가 나오면 이 위치로 바꿔 읽는다.",
           dir=tilde(skill_dir), name=name),
        tr("2. 사용자가 함께 준 인자는 이 메시지의 ARGUMENTS 에 있다."),
        tr("3. 작업이 끝나면 한 번만 묻는다. 이 레포에서 계속 쓰기(`harnist recall skill:{name} --attach .`), 전역으로 되돌리기(`harnist recall skill:{name} --global`), 지금처럼 두기 중 무엇을 원하는지. `harnist` 는 `python3 \"$(cat ~/.claude/.harnist/home)/harnist.py\"` 이다.",
           name=name),
        "",
    ])
    (d / "SKILL.md").write_text(f"---\n{fm}---\n\n{body}")
    return d


def stub_info(name: str) -> dict:
    f = claude_home() / "skills" / name / "SKILL.md"
    fm = frontmatter(f) if f.exists() else {}
    if not fm.get("harnist-stub"):
        raise HarnistError(tr("skill:{name} 은 harnist 스텁이 아님 (보관된 적 없거나 이미 복귀됨)", name=name))
    return fm


def recall(item: str, to_global: bool, repos: list[Path], dry: bool = False) -> list[str]:
    """보관된 스킬을 다시 쓴다 — 레포에 연결하거나 전역으로 되돌린다."""
    kind, _, name = item.partition(":")
    if kind != "skill":
        raise HarnistError(tr("recall 은 스텁이 남는 skill 만 받는다 — 에이전트·플러그인·MCP 는 harnist attach <모듈> <레포> 로 연결한다"))
    fm = stub_info(name)
    module = fm["harnist-stub"]
    reg = load_registry([DEFAULT_REGISTRY])
    if module not in reg:
        raise HarnistError(tr("보관 모듈 {module} 이 레지스트리에 없음", module=module))
    m = reg[module]
    log = []
    for r in repos:
        log.append(tr("연결: {repo} ← {module}", repo=tilde(r.expanduser().resolve()), module=module))
        if not dry:
            mp = attach(r, module) if not ((r.expanduser() / "harness.yaml").exists() and module in
                                           (load_yaml(r.expanduser() / "harness.yaml").get("modules") or [])) else r.expanduser() / "harness.yaml"
            rc = main(["generate", "-m", str(mp)])
            if rc:
                log.append(tr("  ! 생성 중단 — 이 레포에서 /harnist:sync 로 마무리"))
    if to_global:
        if fm.get("harnist-origin") == "module":
            log.append(tr("전역 복귀: 스텁 제거 후 글로벌 매니페스트에 {module} 추가·재생성", module=module))
            if not dry:
                for sk in m.spec.get("skills", []):
                    sd = claude_home() / "skills" / sk
                    if (sd / "SKILL.md").exists() and frontmatter(sd / "SKILL.md").get("harnist-stub"):
                        shutil.rmtree(sd)
                _manifest_modules_edit(GLOBAL_MANIFEST, add=module)
                plan = build_plan(GLOBAL_MANIFEST, claude_home())
                actions, conflicts = compute_actions(plan, read_ledger(plan))
                if conflicts:
                    raise HarnistError(tr("글로벌 재생성 충돌: {conflicts}", conflicts="; ".join(conflicts)))
                apply(plan, actions, read_ledger(plan))
                write_lock(plan)
        else:
            src = m.source / "skills" / name
            log.append(tr("전역 복귀: {src} → ~/.claude/skills/{name} (스텁 교체, 보관본은 레지스트리에 남김)", src=tilde(src), name=name))
            if not dry:
                dst = claude_home() / "skills" / name
                shutil.rmtree(dst)
                shutil.copytree(src, dst)
    return log


def demote(item: str, to: str | None, repos: list[Path], dry: bool = False) -> list[str]:
    """전역(~/.claude)에 붙은 것을 레지스트리 모듈로 내리고, 필요한 레포에만 연결한다.

    item: skill:<name> | agent:<name> | plugin:<id> | module:<layer/name>(글로벌 매니페스트의 모듈)
    """
    log: list[str] = []
    kind, _, name = item.partition(":")
    if kind not in ("skill", "agent", "plugin", "mcp", "module"):
        raise HarnistError(tr("내릴 수 없는 종류: {kind} (skill·agent·plugin·mcp·module) — 훅은 settings.json 에서 직접 옮긴다", kind=kind))
    reg_root = DEFAULT_REGISTRY

    if kind == "module":
        if name not in (load_yaml(GLOBAL_MANIFEST).get("modules") or []):
            raise HarnistError(tr("글로벌 매니페스트에 {name} 이 없음", name=name))
        target = name
    else:
        if not to or to.split("/")[0] not in ("base", "domain"):
            raise HarnistError(tr("--to base/… 또는 domain/… 으로 보관할 모듈 이름을 정한다"))
        target = to
        items = {(i["kind"], i["name"]): i for i in global_items()}
        it = items.get((kind, name))
        if not it:
            raise HarnistError(tr("전역에 없는 항목: {item}", item=item))
        if it.get("stub"):
            raise HarnistError(tr("{item} 은 이미 {module} 로 보관된 스텁", item=item, module=it["stub"]))
        if it["module"]:
            raise HarnistError(tr("{item} 은 harnist 글로벌 모듈 {module} 의 일부 — module:{module} 로 내린다", item=item, module=it["module"]))
        d = reg_root / target
        spec = load_yaml(d / "module.yaml") if (d / "module.yaml").exists() else {
            "name": target, "layer": target.split("/")[0], "description": it["desc"][:80]}
        if spec.get("source"):
            raise HarnistError(tr("{module} 은 외부 원본을 가리키는 모듈이라 내용을 더할 수 없음", module=target))
        if kind == "mcp":
            cfg = (claude_json().get("mcpServers") or {})[name]
            loose = " ".join(map(str, cfg.get("args") or [])) + " " + str(cfg.get("url", ""))
            if it.get("secret") or SECRET_RE.search(loose) or re.search(r"[?&](token|key|secret)=", loose, re.I):
                raise HarnistError(tr(
                    "MCP {name} 설정에 env/headers 가 있어 레지스트리(git)에 그대로 옮기지 않는다 — 값을 ${{환경변수}} 로 바꾼 설정으로 모듈을 직접 만들고 claude mcp remove 로 전역에서 뗀다",
                    name=name))
            spec.setdefault("mcp", {})[name] = (claude_json().get("mcpServers") or {})[name]
        elif kind == "plugin":
            mk = name.split("@")[-1]
            try:
                src = json.loads((claude_home() / "settings.json").read_text()).get("extraKnownMarketplaces", {}).get(mk, {}).get("source")
            except FileNotFoundError:
                src = None
            entry = {"id": name, **({"marketplace": src} if src else {})}
            spec.setdefault("plugins", [])
            if all(p["id"] != name for p in spec["plugins"]):
                spec["plugins"].append(entry)
        else:
            key = kind + "s"
            if name in spec.get(key, []):
                raise HarnistError(tr("{module} 에 이미 {name} 이 있음", module=target, name=name))
            spec.setdefault(key, []).append(name)
            rules = spec.setdefault("rewrites", [])
            if not any("${install}" in r.get("replace", "") for r in rules):
                home = re.escape(str(claude_home()))
                rules.append({"pattern": rf"(?:~/\.claude|{home})/(skills|agents)/", "replace": r"${install}/\1/"})
        if kind in ("skill", "agent"):
            src_path = claude_home() / ("skills" if kind == "skill" else "agents") / (name if kind == "skill" else f"{name}.md")
            leaks = find_secrets([src_path])
            if leaks:
                raise HarnistError(tr("비밀값으로 보이는 문자열이 있어 git 레지스트리로 복사하지 않는다 — 환경변수로 바꾼 뒤 다시 실행: {hits}",
                                      hits="; ".join(leaks)))
        log.append(tr("보관: {kind} {name} → 레지스트리 {module}", kind=kind, name=name, module=target))
        if not dry:
            d.mkdir(parents=True, exist_ok=True)
            if kind == "skill":
                shutil.copytree(claude_home() / "skills" / name, d / "skills" / name,
                                ignore=shutil.ignore_patterns(*SKIP_NAMES, "*.pyc", *SECRET_FILES))
            elif kind == "agent":
                (d / "agents").mkdir(exist_ok=True)
                shutil.copy2(claude_home() / "agents" / f"{name}.md", d / "agents" / f"{name}.md")
            (d / "module.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False))

    # 필요한 레포에만 연결
    for r in repos:
        log.append(tr("연결: {repo} ← {module}", repo=tilde(r.expanduser().resolve()), module=target))
        if not dry:
            mp = attach(r, target)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = main(["generate", "-m", str(mp)])
            if rc:
                log.append(tr("  ! 생성 중단 — 이 레포에서 /harnist:sync 로 마무리: {output}", output=buf.getvalue().strip()))

    # 전역에서 떼기 (보관·연결 뒤에)
    if kind == "module":
        log.append(tr("전역 해제: 글로벌 매니페스트에서 {name} 제거 후 재생성", name=name))
        if not dry:
            _manifest_modules_edit(GLOBAL_MANIFEST, remove=name)
            plan = build_plan(GLOBAL_MANIFEST, claude_home())
            actions, conflicts = compute_actions(plan, read_ledger(plan))
            if conflicts:
                raise HarnistError(tr("글로벌 재생성 충돌: {conflicts}", conflicts="; ".join(conflicts)))
            apply(plan, actions, read_ledger(plan))
            write_lock(plan)
            mod = load_registry([reg_root])[name]
            for sk in mod.spec.get("skills", []):
                link = claude_home() / "skills" / sk
                # 링크면 그 너머(예: ~/.agents/skills)가 이미 전역용으로 경로가 맞춰진 공용 사본이다
                sd = link.resolve() if link.is_symlink() else mod.source / "skills" / sk
                write_stub(sk, name, sd, str(frontmatter(sd / "SKILL.md").get("description", "")), "module")
                log.append(tr("스텁: ~/.claude/skills/{name} (/{name} 로 부르면 보관본을 읽어 수행, 상시 비용 0)", name=sk))
    elif kind == "mcp":
        log.append(tr("전역 해제: claude mcp remove {name} -s user", name=name))
        if not dry:
            r = subprocess.run(["claude", "mcp", "remove", name, "-s", "user"], capture_output=True, text=True)
            if r.returncode:
                log.append(tr("  ! 해제 실패 — 직접 실행: claude mcp remove {name} -s user ({error})", name=name, error=r.stderr.strip()[:200]))
    elif kind == "plugin":
        log.append(tr("전역 해제: claude plugin disable {name} --scope user", name=name))
        if not dry:
            r = subprocess.run(["claude", "plugin", "disable", name, "--scope", "user"], capture_output=True, text=True)
            if r.returncode:
                log.append(tr("  ! 비활성화 실패 — 직접 실행: claude plugin disable {name} --scope user ({error})", name=name, error=r.stderr.strip()[:200]))
    else:
        src = claude_home() / ("skills" if kind == "skill" else "agents") / (name if kind == "skill" else f"{name}.md")
        import time
        trash = Path(__file__).resolve().parent / ".trash" / time.strftime("%Y%m%d-%H%M%S") / src.parent.name
        log.append(tr("전역 해제: {src} → {dst} (백업 후 제거)", src=tilde(src), dst=tilde(trash / src.name)))
        if not dry:
            trash.mkdir(parents=True, exist_ok=True)
            desc = str(frontmatter(src / "SKILL.md" if kind == "skill" else src).get("description", ""))
            shutil.move(str(src), str(trash / src.name))
            if kind == "skill":
                write_stub(name, target, reg_root / target / "skills" / name, desc, "skill")
        if kind == "skill":
            log.append(tr("스텁: ~/.claude/skills/{name} (/{name} 로 부르면 보관본을 읽어 수행, 상시 비용 0)", name=name))
    return log


# ---------------------------------------------------------------- view

PRUNE = {"node_modules", "Pods", "DerivedData", "build", "dist", "_workspace", "__pycache__", "venv"}


def tilde(p: Path | str) -> str:
    s, h = str(p), str(Path.home())
    return "~" + s[len(h):] if s == h or s.startswith(h + os.sep) else s


def find_manifests(root: Path, depth: int = 6) -> list[Path]:
    found: list[Path] = []

    def walk(d: str, n: int) -> None:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        if any(e.name == "harness.yaml" and e.is_file() for e in entries):
            found.append(Path(d) / "harness.yaml")
        if n:
            for e in entries:
                if e.is_dir(follow_symlinks=False) and e.name not in PRUNE and not e.name.startswith("."):
                    walk(e.path, n - 1)

    walk(str(root), depth)
    return found


def find_repos(root: Path, depth: int = 4) -> list[Path]:
    """root 아래 레포(하네스 유무 무관). 표지(.git·.claude·CLAUDE.md·harness.yaml)가 있는 폴더를 repo_of 로 묶는다."""
    cache: dict = {}
    found: set[str] = set()

    def walk(d: Path, n: int) -> None:
        try:
            entries = [e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        if d != root and _is_project(d):
            found.add(repo_of(str(d), cache))
        if n:
            for e in entries:
                if e.name not in PRUNE and not e.name.startswith("."):
                    walk(Path(e.path), n - 1)

    walk(root, depth)
    return sorted(Path(p).expanduser() for p in found if p not in ("?", "(임시 폴더)"))


def local_inventory(d: Path) -> dict:
    return {
        "skills": sorted(x.parent.name for x in d.glob(".claude/skills/*/SKILL.md")),
        "agents": sorted(x.stem for x in d.glob(".claude/agents/*.md")),
        "mcp": sorted((read_json(d / ".mcp.json").get("mcpServers") or {}).keys()),
        "plugins": sorted(k for k, v in (read_json(d / ".claude/settings.json").get("enabledPlugins") or {}).items() if v),
        "claude_md": (d / "CLAUDE.md").exists(),
    }


def module_record(m: Module, key: str) -> dict:
    s = m.spec
    return {
        "key": key, "name": m.name, "layer": m.layer, "description": s.get("description", ""),
        "requires": s.get("requires", []), "skills": s.get("skills", []), "agents": s.get("agents", []),
        "plugins": [p["id"] for p in s.get("plugins", [])], "mcp": list((s.get("mcp") or {}).keys()),
        "settings": bool(s.get("settings")), "source": tilde(m.source), "dir": tilde(m.dir),
        "claude_md": (s.get("claude_md") or "").strip(), "used_by": [],
    }


def collect_state(root: Path, extra: list[Path] = ()) -> dict:
    seen, manifests = set(), []
    for mp in [*find_manifests(root), *extra]:
        r = mp.resolve()
        if r not in seen:
            seen.add(r)
            manifests.append(r)
    modules: dict[str, dict] = {}
    projects = []
    for mp in manifests:
        try:
            man = load_yaml(mp)
        except Exception:
            continue
        if not isinstance(man, dict) or "modules" not in man:
            continue
        base = mp.parent
        rec = {"id": str(base), "name": base.name, "path": tilde(base), "scope": man.get("scope", "project"),
               "spawn": man.get("spawn") or {}, "teams": man.get("teams") or {}, "modules": [], "roots": [],
               "edges": [], "issues": [], "status": "clean", "files": 0}
        projects.append(rec)
        try:
            plan = build_plan(mp)
        except HarnistError as e:
            rec.update(status="error", issues=[str(e)])
            continue
        key = lambda m: f"{base.name}:{m.name}" if m.layer == "project" else m.name
        names = {m.name: key(m) for m in plan.modules}
        for m in plan.modules:
            k = key(m)
            modules.setdefault(k, module_record(m, k))["used_by"].append(rec["id"])
            rec["modules"].append(k)
            rec["edges"] += [[k, names[d]] for d in m.requires]
        rec["roots"] = [names[r] for r in man.get("modules", [])]
        rec["files"] = len(plan.files)
        ledger = read_ledger(plan)
        actions, conflicts = compute_actions(plan, ledger)
        rec["issues"] = [f"{k} {r}" for k, r in actions if k != "adopt"] + conflicts + [tr("락 {diff}", diff=d) for d in lock_diff(plan)]
        if not ledger:
            rec["status"] = "new"
        elif conflicts:
            rec["status"] = "conflict"
        elif rec["issues"]:
            rec["status"] = "drift"
    try:
        for m in load_registry([DEFAULT_REGISTRY]).values():
            modules.setdefault(m.name, module_record(m, m.name))
    except HarnistError:
        pass
    managed = {Path(p["id"]) for p in projects}
    others = []
    for r in find_repos(root):
        if r in managed or (r / "harness.yaml").exists():
            continue
        inv = local_inventory(r)
        try:
            mtime = max((r / x).stat().st_mtime for x in (".", ".claude", ".git") if (r / x).exists())
        except ValueError:
            mtime = 0
        others.append({"id": str(r), "name": r.name, "path": tilde(r), "inv": inv, "mtime": mtime})
    others.sort(key=lambda x: -x["mtime"])
    return {"root": tilde(root), "registry": tilde(DEFAULT_REGISTRY), "projects": projects, "others": others,
            "modules": sorted(modules.values(), key=lambda x: (-LAYERS[x["layer"]], x["name"]))}


# ---------------------------------------------------------------- bench


BENCH_PROMPT = "ok 라고만 답해"
PROFILES = {"user": None, "agent": "haiku"}  # user = 설정의 기본 모델, agent = 워커로 흔한 모델


def bench_dir() -> Path:
    d = claude_home() / ".harnist" / "bench"
    d.mkdir(parents=True, exist_ok=True)
    return d


def probe_dir() -> Path:
    d = claude_home() / ".harnist" / "probe"  # 프로젝트 설정이 섞이지 않는 빈 폴더 — 전역 부담만 잰다
    d.mkdir(parents=True, exist_ok=True)
    return d


def parse_debug(path: Path) -> dict:
    import datetime
    rows = []
    for line in path.read_text(errors="ignore").splitlines():
        if re.match(r"\d{4}-\d\d-\d\dT", line):
            try:
                rows.append((datetime.datetime.fromisoformat(line[:23]), line[24:]))
            except ValueError:
                pass
    out = {"skills_n": None, "skills_chars": None, "skills_budget": None, "hooks_n": None, "mcp_ok": [], "mcp_fail": []}
    if not rows:
        return out
    t0 = rows[0][0]
    at = lambda key: next(((t - t0).total_seconds() for t, l in rows if key in l), None)
    out["to_request_s"] = at("[API REQUEST]")
    out["first_token_s"] = at("Stream started")
    out["end_s"] = (rows[-1][0] - t0).total_seconds()
    out.update(timeline(rows, out["to_request_s"]))
    for _, l in rows:
        m = re.search(r"Skill listing over budget: (\d+) skills, (\d+) chars > (\d+) budget", l)
        if m:  # 이 경고가 찍히면 모델이 보는 스킬 설명이 잘린다
            out["skills_n"], out["skills_chars"], out["skills_budget"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
        m = re.search(r"Sending (\d+) skills via attachment", l)
        if m and out["skills_n"] is None:
            out["skills_n"] = int(m.group(1))
        m = re.search(r"Registered (\d+) hooks", l)
        if m:
            out["hooks_n"] = int(m.group(1))
        m = re.search(r'MCP server "([^"]+)": Successfully connected', l)
        if m and m.group(1) not in out["mcp_ok"]:
            out["mcp_ok"].append(m.group(1))
        m = re.search(r'MCP server "([^"]+)".*(timed out|[Ff]ailed to connect|Connection failed)', l)
        if m and m.group(1) not in out["mcp_fail"]:
            out["mcp_fail"].append(m.group(1))
    return out


def _classify(line: str, prev: str) -> tuple[str, str]:
    """긴 공백 뒤에 찍힌 줄 = 기다리던 일. 하네스가 좌우하는 일(훅·로컬 MCP)과 밖의 일(네트워크)을 가른다."""
    m = re.search(r"Failed to fetch ([^:]+):.*?timeout of (\d+)ms", line)
    if m:
        return "net_timeout", m.group(1).strip()
    if "installPluginsForHeadless" in prev or "installPluginsForHeadless" in line:
        return "plugins", ""
    m = re.search(r'MCP server "([^"]+)": Successfully connected \(transport: ([\w-]+)\)', line)
    if m:
        return ("mcp" if m.group(2) == "stdio" else "net"), m.group(1)
    if re.search(r"\[claudeai-mcp\]|claude\.ai proxy|mcp-registry|\[Bootstrap\]|Fetch|fetch|AxiosError|https?://", line):
        tag = re.search(r"\[([\w.-]+)\]", line[8:])
        return "net", tag.group(1) if tag else line[8:60].strip()
    if "Hook" in line:
        m = re.search(r"\((.+?)\) (?:success|completed)", line)
        return "hook", (m.group(1) if m else line[8:60]).strip()
    return "other", line[8:60].strip()


def timeline(rows: list, req_s: float | None, min_gap: float = 0.3) -> dict:
    """시작 → 첫 API 요청 구간을 로컬 기동 / 네트워크 대기 / 헤드리스 플러그인 점검으로 나누고 긴 공백의 원인을 남긴다."""
    if req_s is None or not rows:
        return {"seg": None, "causes": []}
    t0 = rows[0][0]
    net = plug = 0.0
    causes = []
    prev_t, prev_l = t0, ""
    in_plugins = False
    for t, l in rows:
        el = (t - t0).total_seconds()
        if el > req_s:
            break
        if "installPluginsForHeadless: starting" in l:
            in_plugins = True
        gap = (t - prev_t).total_seconds()
        if gap >= min_gap:
            kind, name = ("plugins", "") if in_plugins and "Failed to fetch" not in l else _classify(l, prev_l)
            causes.append({"kind": kind, "x": name, "s": round(gap, 2), "at": round(el, 2)})
            if kind in ("net", "net_timeout"):
                net += gap
            elif kind == "plugins":
                plug += gap
        if in_plugins and re.search(r"output styles loaded|installPluginsForHeadless: (done|complete|finished)", l):
            in_plugins = False
        prev_t, prev_l = t, l
    merged: dict = {}
    for c in causes:  # 같은 원인은 하나로 합친다
        m = merged.setdefault((c["kind"], c["x"]), {**c, "s": 0.0})
        m["s"] = round(m["s"] + c["s"], 2)
    return {"seg": {"local": round(max(0.0, req_s - net - plug), 2), "net": round(net, 2), "plugins": round(plug, 2)},
            "causes": sorted(merged.values(), key=lambda c: -c["s"])[:6]}


def run_probe(label: str, model: str | None) -> dict:
    import time
    claude = shutil.which("claude")
    if not claude:
        raise HarnistError(tr("claude CLI 를 PATH 에서 찾지 못함"))
    dbg = probe_dir() / f"{label}.debug.log"
    dbg.unlink(missing_ok=True)
    argv = [claude, "-p", BENCH_PROMPT, "--output-format", "json", "--debug-file", str(dbg)]
    if model:
        argv += ["--model", model]
    t = time.time()
    r = subprocess.run(argv, cwd=probe_dir(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    wall = time.time() - t
    res = {}
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            res = json.loads(line)
            break
        except ValueError:
            continue
    if not res:
        raise HarnistError(tr("{label} 프로브 실패: {output}", label=label, output=(r.stderr or r.stdout)[-300:]))
    u = res.get("usage") or {}
    ctx = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"label": label, "model": next(iter(res.get("modelUsage") or {}), model or tr("기본")),
            "wall_s": round(wall, 2), "ctx_tokens": ctx, "out_tokens": int(u.get("output_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0), "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cost_usd": float(res.get("total_cost_usd") or 0), "ttft_ms": res.get("ttft_ms"),
            **parse_debug(dbg)}


def session_counts(days: int) -> dict:
    """최근 N일 메인 세션 수와 Agent 도구로 띄운 서브에이전트 수 (측정용 프로브 세션 제외)."""
    import time
    cutoff = time.time() - days * 86400
    sessions = spawns = 0
    oldest = time.time()
    for f in (claude_home() / "projects").glob("*/*.jsonl"):
        if "harnist-probe" in f.parent.name or f.stat().st_mtime < cutoff:
            continue
        sessions += 1
        oldest = min(oldest, f.stat().st_mtime)
        for line in f.open(errors="ignore"):
            if '"tool_use"' in line and ('"name":"Agent"' in line or '"name":"Task"' in line):
                spawns += line.count('"name":"Agent"') + line.count('"name":"Task"')
    # 세션 기록은 Claude Code 설정 cleanupPeriodDays(기본 30일)만큼만 남는다 — 실제로 덮는 기간을 함께 낸다
    covered = min(days, max(1, round((time.time() - oldest) / 86400))) if sessions else 0
    return {"days": days, "sessions": sessions, "agent_spawns": spawns, "covered_days": covered}


def fit_summary(projects: list[dict]) -> dict:
    """레포별 적합도 = 모든 세션에 실리는 전역 항목 중 그 레포가 실제로 쓴 비율. 낮을수록 세션마다 상관없는 도구를 싣고 다닌다."""
    xs = [p["fit"]["ratio"] for p in projects if p.get("fit") and p["fit"]["ratio"] is not None]
    return {"repos": len(xs), "avg": round(sum(xs) / len(xs), 3) if xs else None}


def static_summary(rows: list[dict]) -> dict:
    by = {}
    for r in rows:
        by.setdefault(r["verdict"], []).append(r)
    return {"items": len([r for r in rows if r["kind"] != "hook"]),
            "always_chars": sum(r["always_chars"] for r in rows),
            "verdicts": {v: {"n": len(g), "chars": sum(r["always_chars"] for r in g)} for v, g in by.items()},
            "stubs": len(by.get("보관됨", []))}


def recommendations(rows: list[dict], root: Path) -> list[dict]:
    recs, seen = [], set()
    for r in rows:
        if r["verdict"] not in ("미사용", "제한", "호출 기록 없음") or r["kind"] == "hook" or r.get("stub"):
            continue
        item = f"module:{r['module']}" if r.get("module") else f"{r['kind']}:{r['name']}"
        if item in seen:
            continue
        seen.add(item)
        repos = []
        if r["verdict"] == "제한":
            for k in r["repos"]:
                p = Path(k).expanduser()
                if p.is_dir() and (root in p.parents or p == root) and p != root:
                    repos.append(k)
        slug = re.sub(r"[^a-z0-9-]+", "-", r["name"].split("@")[0].lower()).strip("-")
        blocked = r["kind"] == "mcp" and r.get("secret")
        recs.append({
            "item": item, "kind": r["kind"], "name": r["name"], "verdict": r["verdict"],
            "to": None if r.get("module") else f"domain/{slug}", "attach": repos,
            "chars": r["always_chars"], "sessions": r["sessions"],
            "action": "수동(비밀값 포함 MCP)" if blocked else
                      {"skill": "보관+스텁", "agent": "보관", "plugin": "비활성+보관", "mcp": "보관 후 제거"}.get(r["kind"], "보관")
                      + (f" → {len(repos)}개 레포 연결" if repos else ""),
            "checked": not blocked and r["verdict"] != "호출 기록 없음",
            "blocked": blocked,
        })
    return sorted(recs, key=lambda x: -x["chars"])


def latest_skill_listing() -> list[dict]:
    """세션 기록의 skill_listing 첨부 = 모델이 실제로 받은 스킬 목록. 측정 프로브 기록 중 가장 긴 것(잘리지 않은 것)을 쓴다."""
    files = sorted((claude_home() / "projects").glob("*/*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    # 가장 최근 측정 한 쌍(사용자·에이전트) 중 긴 쪽 = 잘리지 않은 목록. 옛 측정은 이미 바뀐 설정을 담고 있어 쓰지 않는다
    probe = [f for f in files if "harnist-probe" in f.parent.name][:2] or files[:20]
    best = ""
    for f in probe:
        for line in f.open(errors="ignore"):
            if '"skill_listing"' not in line:
                continue
            try:
                a = json.loads(line).get("attachment") or {}
            except ValueError:
                continue
            if a.get("type") == "skill_listing" and len(a.get("content", "")) > len(best):
                best = a["content"]
    out = []
    for m in re.finditer(r"(?ms)^- (\S+?): (.*?)(?=^- \S+?: |\Z)", best):
        out.append({"name": m.group(1), "chars": len(m.group(2).strip())})
    return out


def skill_overrides() -> dict:
    return read_json(claude_home() / "settings.json").get("skillOverrides") or {}


def listing_report(days: int = 90) -> list[dict]:
    """목록에 실린 스킬마다 출처·설명 글자 수·실사용·현재 오버라이드."""
    use = scan_usage(days)
    user_dirs = {p.parent.name for p in (claude_home() / "skills").glob("*/SKILL.md")}
    plugins = {k.split("@")[0] for k, v in (read_json(claude_home() / "settings.json").get("enabledPlugins") or {}).items() if v}
    ov = skill_overrides()
    rows = []
    for it in latest_skill_listing():
        n = it["name"]
        prefix = n.split(":")[0] if ":" in n else None
        origin = "user" if n in user_dirs else ("plugin" if prefix in plugins else "builtin")
        u = use.get(("skill", n)) or {}
        rows.append({**it, "origin": origin, "calls": sum((u.get("repos") or {}).values()),
                     "sessions": len(u.get("sessions") or ()), "override": ov.get(n)})
    return sorted(rows, key=lambda r: -r["chars"])


def listing_recs(rows: list[dict], min_chars: int = 80) -> list[dict]:
    """안 쓰는 내장·플러그인 스킬은 설명만 숨긴다(skillOverrides: name-only) — 이름은 남아 부를 수 있다."""
    return [{"item": f"listing:{r['name']}", "kind": "listing", "name": r["name"], "verdict": "미사용", "to": None, "attach": [],
             "chars": r["chars"], "sessions": 0, "action": "name-only", "checked": True, "blocked": False, "origin": r["origin"]}
            for r in rows if r["origin"] != "user" and not r["calls"] and not r["override"] and r["chars"] >= min_chars]


def set_skill_overrides(names: list[str], value: str | None, log=print) -> None:
    """~/.claude/settings.json 의 skillOverrides 키만 고친다. 고치기 전에 백업한다."""
    import time
    p = claude_home() / "settings.json"
    d = read_json(p)
    bk = claude_home() / ".harnist" / "backups"
    bk.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copy2(p, bk / f"settings-{time.strftime('%Y%m%d-%H%M%S')}.json")
    ov = d.setdefault("skillOverrides", {})
    for n in names:
        if value is None:
            ov.pop(n, None)
        else:
            ov[n] = value
    if not ov:
        d.pop("skillOverrides")
    tmp = p.with_suffix(".json.harnist-tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, p)
    log(tr("skillOverrides 갱신: {names} → {value}", names=", ".join(names), value=value or tr("기본값")))


def run_bench(label: str, root: Path, log=print) -> dict:
    import time
    log(tr("정적 점검: 세션 기록에서 전역 항목 사용을 집계한다"))
    rows = usage_report(90)
    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "label": label, "static": static_summary(rows),
           "recs": [], "sessions": {d: session_counts(d) for d in (30, 90)}, "probes": {}}
    for name, model in PROFILES.items():
        log(tr("{name} 세션 프로브: claude -p ({model}) 실행 중", name=name, model=model or tr("기본 모델")))
        try:
            out["probes"][name] = run_probe(name, model)
            p = out["probes"][name]
            log(tr("  기본 컨텍스트 {tokens:,} 토큰 · ${cost:.4f} · 총 {wall}초", tokens=p["ctx_tokens"], cost=p["cost_usd"], wall=p["wall_s"]))
        except (HarnistError, subprocess.TimeoutExpired) as e:
            log(tr("  실패: {error}", error=e))
    out["recs"] = sorted(recommendations(rows, root) + listing_recs(listing_report(90)), key=lambda x: -x["chars"])
    d = bench_dir()
    f = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.json"
    f.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    if not (d / "baseline.json").exists():
        (d / "baseline.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
        log(tr("첫 측정이라 기준선으로 저장했다"))
    log(tr("저장: {path}", path=tilde(f)))
    return out


def bench_history() -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted(bench_dir().glob("2*.json"))]


def savings(base: dict, cur: dict) -> dict:
    """기준선 대비 절감. 금액은 토큰 감소분 × 기준선의 토큰당 단가 — 실제 청구액 차이는 캐시 상태에 따라 흔들려 참고값으로만 둔다."""
    res = {"per_session": {}, "windows": {}, "why": {}}
    for name in PROFILES:
        b, c = (base or {}).get("probes", {}).get(name), (cur or {}).get("probes", {}).get(name)
        if not (b and c):
            continue
        rate = b["cost_usd"] / b["ctx_tokens"] if b.get("ctx_tokens") else 0
        dtok = b["ctx_tokens"] - c["ctx_tokens"]
        res["per_session"][name] = {"tokens": dtok, "usd": dtok * rate, "billed_usd": b["cost_usd"] - c["cost_usd"],
                                    "local_s": round(((b.get("seg") or {}).get("local") or 0) - ((c.get("seg") or {}).get("local") or 0), 2)}
        res["why"][name] = explain_change(b, c)
    for d, sc in (cur or {}).get("sessions", {}).items():
        u, a = res["per_session"].get("user"), res["per_session"].get("agent")
        res["windows"][str(d)] = {
            "sessions": sc["sessions"], "agent_spawns": sc["agent_spawns"],
            "tokens": (u["tokens"] * sc["sessions"] if u else 0) + (a["tokens"] * sc["agent_spawns"] if a else 0),
            "usd": (u["usd"] * sc["sessions"] if u else 0) + (a["usd"] * sc["agent_spawns"] if a else 0),
        }
    return res


def explain_change(b: dict, c: dict, noise: float = 0.5) -> list[dict]:
    """시간이 늘어난 구간마다 이유를 붙인다. 하네스가 좌우하는 건 로컬 기동뿐이다."""
    out = []
    bs, cs = b.get("seg") or {}, c.get("seg") or {}
    if not bs and b.get("to_request_s") is not None and c.get("to_request_s") is not None:
        # 구간 분해가 없는 옛 기준선 — 시작 구간 전체로만 비교하고 현재 원인을 붙인다
        d = c["to_request_s"] - b["to_request_s"]
        if d > noise:
            out.append({"seg": "start", "d": round(d, 2), "causes": c.get("causes", [])[:3]})
        cs = {}
    for key in ("local", "net", "plugins") if bs else ():
        d = (cs.get(key) or 0) - (bs.get(key) or 0)
        if d > noise:
            out.append({"seg": key, "d": round(d, 2),
                        "causes": [x for x in c.get("causes", []) if
                                   (key == "net" and x["kind"] in ("net", "net_timeout")) or
                                   (key == "plugins" and x["kind"] == "plugins") or
                                   (key == "local" and x["kind"] in ("hook", "mcp", "other"))][:3]})
    if None not in (b.get("first_token_s"), b.get("to_request_s"), c.get("first_token_s"), c.get("to_request_s")):
        d = (c["first_token_s"] - c["to_request_s"]) - (b["first_token_s"] - b["to_request_s"])
        if d > noise:
            out.append({"seg": "api", "d": round(d, 2), "causes": []})
    if None not in (b.get("end_s"), b.get("first_token_s"), c.get("end_s"), c.get("first_token_s")):
        d = (c["end_s"] - c["first_token_s"]) - (b["end_s"] - b["first_token_s"])
        if d > noise:
            out.append({"seg": "tail", "d": round(d, 2), "a": b.get("out_tokens"), "b": c.get("out_tokens"), "causes": []})
    return out


def apply_recs(items: list[dict], log=print) -> None:
    hide = [it["item"].split(":", 1)[1] for it in items if it["item"].startswith("listing:")]
    if hide:
        set_skill_overrides(hide, "name-only", log)
    for it in [x for x in items if not x["item"].startswith("listing:")]:
        try:
            for line in demote(it["item"], it.get("to"), [Path(p).expanduser() for p in it.get("attach") or []]):
                log(line)
        except HarnistError as e:
            log(tr("건너뜀 {item}: {error}", item=it["item"], error=e))
    mark_audit()
    log(tr("점검 완료 기록을 갱신했다"))


# ---------------------------------------------------------------- dashboard actions


def engine_cmd() -> str:
    """claude -p 안에서 엔진을 부를 명령 — OS 마다 파이썬 실행 파일 이름이 다르다."""
    py = "python3" if shutil.which("python3") else "python"
    return f'{py} "{Path(__file__).resolve()}"'


def recommend(repo: Path, use: dict) -> list[dict]:
    """이 레포에서 실제로 쓴 스킬·에이전트·플러그인·MCP 를 제공하는 레지스트리 모듈 = 추천."""
    key = tilde(repo)
    out = []
    live = {f"{i['kind']}:{i['name']}" for i in global_items() if not i.get("stub")}
    for m in load_registry([DEFAULT_REGISTRY]).values():
        s = m.spec
        ids = [f"skill:{n}" for n in s.get("skills", [])] + [f"agent:{n}" for n in s.get("agents", [])] + \
              [f"plugin:{p['id']}" for p in s.get("plugins", [])] + [f"mcp:{n}" for n in (s.get("mcp") or {})]
        if ids and all(i in live for i in ids):
            out.append({"name": m.name, "layer": m.layer, "description": s.get("description", ""), "calls": 0,
                        "reason": "", "recommended": False, "global": True})
            continue
        provides = [("skill", n) for n in s.get("skills", [])] + [("agent", n) for n in s.get("agents", [])] + \
                   [("mcp", n) for n in (s.get("mcp") or {})]
        calls = 0
        hits = []
        for k, n in provides:
            u = use.get((k, n))
            c = sum(v for r, v in (u or {}).get("repos", {}).items() if r == key or r.startswith(key + "/"))
            if c:
                calls += c
                hits.append(f"{n}×{c}")
        for p in s.get("plugins", []):
            pname = p["id"].split("@")[0]
            for (k, n), u in use.items():
                if (k != "mcp" and n.startswith(pname + ":")) or (k == "mcp" and n.startswith(f"plugin_{pname}_")):
                    c = sum(v for r, v in u["repos"].items() if r == key or r.startswith(key + "/"))
                    if c:
                        calls += c
                        hits.append(f"{n}×{c}")
        out.append({"name": m.name, "layer": m.layer, "description": s.get("description", ""), "calls": calls,
                    "reason": ", ".join(hits[:4]), "recommended": calls > 0, "global": False})
    return sorted(out, key=lambda x: (-x["calls"], LAYERS[x["layer"]], x["name"]))


def apply_modules(repo: Path, modules: list[str]) -> tuple[int, str]:
    """harness.yaml 을 만들거나 모듈을 연결하고 generate — 순수 파이썬이라 OS 와 무관하다."""
    log = []
    try:
        for m in modules:
            mp = attach(repo, m)
            log.append(tr("연결: {module}", module=m))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = main(["generate", "-m", str(repo / "harness.yaml")])
        log.append(buf.getvalue().strip())
        return rc, "\n".join(log)
    except HarnistError as e:
        return 2, "\n".join([*log, tr("오류: {error}", error=e)])


DESIGN_PROMPT = """You are running harnist's /harnist:init procedure unattended, on a request from the dashboard. The user cannot answer during the run — do not ask questions; when something is unclear, build less rather than more.
Read the procedure document {skill} with Read and follow it. Do the discovery and lookups in steps 1-3, but wherever the procedure asks the user, use the purpose below instead.
Engine command: {engine}
Project purpose (user input): {purpose}
Constraints: do not modify files outside this folder ({repo}). Do not write .claude/ directly; write .harnist/modules/project/ and harness.yaml, then run generate. If generate reports a conflict with an unmanaged file, do not use --adopt; stop and report it. Finish with a summary of what you created and connected, in 5 lines or fewer, written in the same language as the project purpose."""


class Jobs:
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def start(self, repo: Path, argv: list[str]) -> str:
        import secrets
        import threading
        with self.lock:
            for j in self.jobs.values():
                if j["repo"] == str(repo) and not j["done"]:
                    raise HarnistError(tr("이 레포에서 이미 실행 중인 작업이 있다"))
            jid = secrets.token_hex(6)
            self.jobs[jid] = {"repo": str(repo), "lines": [], "done": False, "rc": None}
        env = {**os.environ, "HARNIST_HOME": str(Path(__file__).resolve().parent)}
        proc = subprocess.Popen(argv, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env)
        threading.Thread(target=self._pump, args=(jid, proc), daemon=True).start()
        return jid

    def start_fn(self, key: str, fn) -> str:
        """파이썬 함수 작업 — fn(log) 의 출력 줄을 같은 방식으로 흘린다."""
        import secrets
        import threading
        with self.lock:
            for j in self.jobs.values():
                if j["repo"] == key and not j["done"]:
                    raise HarnistError(tr("같은 작업이 이미 실행 중이다"))
            jid = secrets.token_hex(6)
            self.jobs[jid] = {"repo": key, "lines": [], "done": False, "rc": None}

        def run():
            j = self.jobs[jid]
            try:
                fn(lambda m: j["lines"].append(str(m)))
                j["rc"] = 0
            except Exception as e:  # 작업 실패는 화면에 보인다
                j["lines"].append(tr("오류: {error}", error=e))
                j["rc"] = 1
            j["done"] = True

        threading.Thread(target=run, daemon=True).start()
        return jid

    def running(self, key: str) -> str | None:
        return next((jid for jid, j in self.jobs.items() if j["repo"] == key and not j["done"]), None)

    def _pump(self, jid: str, proc) -> None:
        j = self.jobs[jid]
        for line in proc.stdout:
            for out in self._render(line):
                j["lines"].append(out)
        j["rc"] = proc.wait()
        j["done"] = True

    @staticmethod
    def _render(line: str) -> list[str]:
        """claude -p --output-format stream-json 한 줄 → 사람이 읽을 줄."""
        try:
            d = json.loads(line)
        except ValueError:
            return [line.rstrip()] if line.strip() else []
        out = []
        if d.get("type") == "assistant":
            for b in (d.get("message") or {}).get("content") or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append(b["text"].strip())
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    hint = inp.get("file_path") or inp.get("command") or inp.get("pattern") or ""
                    out.append(f"· {b.get('name')} {str(hint)[:160]}")
        elif d.get("type") == "result":
            res = str(d.get("result", ""))[:2000]
            out.append(tr("오류로 끝남: {result}", result=res) if d.get("is_error") else tr("끝: {result}", result=res))
        return out

    def get(self, jid: str) -> dict | None:
        return self.jobs.get(jid)


def open_terminal(repo: Path) -> dict:
    """대화형 claude 를 그 폴더에서 연다 — OS 마다 다르다. 실패하면 복사할 명령을 돌려준다."""
    import platform
    import shlex
    system = platform.system()
    fallback = f'cd "{repo}" && claude' if system != "Windows" else f'cd /d "{repo}" && claude'
    try:
        if system == "Darwin":
            script = f"cd {shlex.quote(str(repo))} && claude"
            esc = script.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{esc}"',
                              "-e", 'tell application "Terminal" to activate'])
        elif system == "Windows":
            if shutil.which("wt"):
                subprocess.Popen(["wt", "-d", str(repo), "claude"])
            else:
                subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", fallback])
        else:
            for argv in (["x-terminal-emulator", "-e", "sh", "-c", f"cd {shlex.quote(str(repo))} && claude; exec sh"],
                         ["gnome-terminal", f"--working-directory={repo}", "--", "claude"],
                         ["konsole", "--workdir", str(repo), "-e", "claude"],
                         ["xterm", "-e", "sh", "-c", f"cd {shlex.quote(str(repo))} && claude; exec sh"]):
                if shutil.which(argv[0]):
                    subprocess.Popen(argv)
                    break
            else:
                return {"ok": False, "command": fallback, "reason": tr("지원하는 터미널을 찾지 못함")}
        return {"ok": True, "command": fallback}
    except OSError as e:
        return {"ok": False, "command": fallback, "reason": str(e)}


def serve(root: Path, port: int, extra: list[Path], open_browser: bool, auto_baseline: bool = True, demo: bool = False,
          aliases: list[tuple[str, str]] = ()) -> None:
    import secrets
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse
    page = Path(__file__).resolve().parent / "web" / "index.html"
    token = secrets.token_urlsafe(24)  # 이 서버가 낸 페이지만 실행 요청을 보낼 수 있다
    audit_cache: dict = {}
    jobs = Jobs()

    def usage_cached() -> dict:
        if time.time() - audit_cache.get("ut", 0) > 60:
            audit_cache.update(ut=time.time(), use=scan_usage(90))
        return audit_cache["use"]

    def known_repo(raw: str) -> Path:
        """실행 대상은 지도 루트 아래의 실제 폴더만."""
        for real, shown in aliases:  # 데모 화면의 별칭 경로를 실제 경로로 되돌린다
            if raw == shown or raw.startswith(shown + "/"):
                raw = real + raw[len(shown):]
                break
        p = Path(raw).expanduser().resolve()
        if not p.is_dir() or (root != p and root not in p.parents):
            raise HarnistError(tr("지도 루트 밖이거나 없는 폴더: {path}", path=raw))
        return p

    class Handler(BaseHTTPRequestHandler):
        def guard(self, post: bool) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self.send_error(403)  # DNS 리바인딩 방지
                return False
            if post:
                origin = self.headers.get("Origin")
                if self.headers.get("X-Harnist-Token") != token or (origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost")):
                    self.send_error(403)  # 다른 웹페이지가 보낸 요청(CSRF) 차단
                    return False
            return True

        def reply(self, body, ctype="application/json", code=200):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            if aliases and ctype == "application/json":  # 데모: 임시 폴더 경로를 읽기 좋은 이름으로
                text = body.decode()
                for a, b in aliases:
                    text = text.replace(a, b)
                body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.guard(False):
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/api/state":
                    return self.reply({**collect_state(root, extra), "demo": demo})
                if u.path == "/api/audit":
                    if "fresh" in q or time.time() - audit_cache.get("t", 0) > 60:
                        days = 90
                        use = scan_usage(days)
                        audit_cache.update(ut=time.time(), use=use)
                        rows = usage_report(days)
                        pa = project_audit(rows, use)
                        audit_cache.update(t=time.time(), body=json.dumps({
                            "days": days, "rows": rows, "projects": pa, "fit": fit_summary(pa), "demo": demo,
                            "snapshot": read_json(audit_path()).get("at"), "new": new_since_audit(),
                            "harnist": tilde(Path(__file__).resolve().parent),
                        }, ensure_ascii=False).encode())
                    return self.reply(audit_cache["body"])
                if u.path == "/api/recommend":
                    repo = known_repo(q.get("repo", [""])[0])
                    return self.reply({"modules": recommend(repo, usage_cached()),
                                       "claude": bool(shutil.which("claude"))})
                if u.path == "/api/bench":
                    hist = bench_history()
                    base = read_json(bench_dir() / "baseline.json") or None
                    cur = hist[-1] if hist else None
                    return self.reply({"baseline": base, "latest": cur, "history": [
                        {"at": h["at"], "label": h["label"], **{k: v.get("ctx_tokens") for k, v in h.get("probes", {}).items()}}
                        for h in hist[-12:]], "savings": savings(base, cur) if base and cur else None,
                        "running": jobs.running("bench"), "claude": bool(shutil.which("claude"))})
                if u.path.startswith("/api/job/"):
                    j = jobs.get(u.path.rsplit("/", 1)[-1])
                    return self.reply(j or {"error": tr("없는 작업")}, code=200 if j else 404)
                if u.path == "/i18n.js":
                    return self.reply((Path(__file__).resolve().parent / "web" / "i18n.js").read_bytes(),
                                      "application/javascript; charset=utf-8")
                if u.path in ("/", "/index.html"):
                    return self.reply(page.read_text().replace("__HARNIST_TOKEN__", token).encode(),
                                      "text/html; charset=utf-8")
            except HarnistError as e:
                return self.reply({"error": str(e)}, code=400)
            self.send_error(404)

        def do_POST(self):
            if not self.guard(True):
                return
            if demo:
                return self.reply({"error": "demo"}, code=400)  # 데모는 읽기 전용
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 262144)) or b"{}")
                if self.path == "/api/bench":
                    return self.reply({"job": jobs.start_fn("bench", lambda log: run_bench(body.get("label") or "check", root, log))})
                if self.path == "/api/recs/apply":
                    items = [x for x in body.get("items", []) if isinstance(x, dict) and isinstance(x.get("item"), str)]
                    if not items:
                        raise HarnistError(tr("적용할 추천안을 하나 이상 고른다"))
                    for x in items:
                        for r in x.get("attach") or []:
                            known_repo(r)

                    def go(log):
                        apply_recs(items, log)
                        audit_cache.clear()
                        log(tr("재측정을 시작한다"))
                        run_bench("after", root, log)
                    return self.reply({"job": jobs.start_fn("bench", go)})
                repo = known_repo(body.get("repo", ""))
                if self.path == "/api/apply":
                    mods = [m for m in body.get("modules", []) if isinstance(m, str)]
                    if not mods:
                        raise HarnistError(tr("연결할 모듈을 하나 이상 고른다"))
                    rc, log = apply_modules(repo, mods)
                    return self.reply({"rc": rc, "log": log})
                if self.path == "/api/generate":
                    rc, log = apply_modules(repo, [])
                    return self.reply({"rc": rc, "log": log})
                if self.path == "/api/design":
                    claude = shutil.which("claude")
                    if not claude:
                        raise HarnistError(tr("claude CLI 를 PATH 에서 찾지 못함"))
                    purpose = str(body.get("purpose", "")).strip()[:4000]
                    if not purpose:
                        raise HarnistError(tr("프로젝트 목적을 적는다"))
                    prompt = DESIGN_PROMPT.format(skill=Path(__file__).resolve().parent / "plugin/skills/init/SKILL.md",
                                                  engine=engine_cmd(), purpose=purpose, repo=repo)
                    py = "python3" if shutil.which("python3") else "python"
                    argv = [claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
                            "--permission-mode", "acceptEdits",
                            "--allowedTools", f"Read,Write,Edit,Glob,Grep,Bash({py}:*),Bash(ls:*),Bash(git log:*)"]
                    return self.reply({"job": jobs.start(repo, argv)})
                if self.path == "/api/terminal":
                    return self.reply(open_terminal(repo))
            except HarnistError as e:
                return self.reply({"error": str(e)}, code=400)
            except ValueError:
                return self.reply({"error": tr("잘못된 요청")}, code=400)
            self.send_error(404)

        def log_message(self, *_):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(tr("harnist view — {url}  (루트 {root}, 종료 Ctrl-C)", url=url, root=tilde(root)), flush=True)
    if auto_baseline and not (bench_dir() / "baseline.json").exists() and shutil.which("claude"):
        print(tr("기준선 측정이 없어 처음 한 번 자동으로 잰다 (claude -p 두 번, 점검 탭에서 진행 상황 확인)"), flush=True)
        jobs.start_fn("bench", lambda log: run_bench("baseline", root, log))
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- cli


def remember_home() -> None:
    """훅·스킬이 엔진 위치를 찾도록 기록한다 — 경로를 하드코딩하지 않기 위해."""
    try:
        d = claude_home() / ".harnist"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "home"
        here = str(Path(__file__).resolve().parent)
        if not f.exists() or f.read_text().strip() != here:
            f.write_text(here + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    if argv is None:
        remember_home()
    ap = argparse.ArgumentParser(prog="harnist", description=tr("Tuist식 Claude Code 하네스 생성기"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("generate", "check", "graph", "lock"):
        sp = sub.add_parser(name)
        sp.add_argument("-m", "--manifest", default="harness.yaml")
        sp.add_argument("--out", help=tr("출력 루트 (기본: 매니페스트 디렉터리)"))
        if name == "generate":
            sp.add_argument("--dry-run", action="store_true")
            sp.add_argument("--frozen", action="store_true", help=tr("harness.lock 과 다르면 중단"))
            sp.add_argument("--adopt", action="store_true", help=tr("관리 밖 기존 파일을 덮어쓰고 편입"))
            sp.add_argument("--force", action="store_true", help=tr("수동 수정된 생성물도 덮어쓰기"))
        if name == "graph":
            sp.add_argument("--format", choices=["mermaid", "json"], default="mermaid")
    ls = sub.add_parser("list")
    ls.add_argument("--registry", action="append")
    ip = sub.add_parser("init", help=tr("빈 harness.yaml 생성"))
    ip.add_argument("dir", nargs="?", default=".")
    ip.add_argument("--modules", nargs="*", default=[])
    pp = sub.add_parser("promote", help=tr("project 모듈을 공유 레지스트리로 승격"))
    pp.add_argument("-m", "--manifest", default="harness.yaml")
    pp.add_argument("name")
    pp.add_argument("--to", required=True)
    au = sub.add_parser("audit", help=tr("전역 점검 스냅샷 — --mark 로 현재 상태를 점검 완료로 기록"))
    au.add_argument("--mark", action="store_true")
    us = sub.add_parser("usage", help=tr("전역 스킬·에이전트·플러그인의 레포별 실제 사용(세션 기록)"))
    us.add_argument("--days", type=int, default=90)
    us.add_argument("--json", action="store_true")
    dm = sub.add_parser("demote", help=tr("전역 항목을 레지스트리 모듈로 내리고 필요한 레포에만 연결"))
    dm.add_argument("item", help=tr("skill:<이름> | agent:<이름> | plugin:<id> | module:<계층/이름>"))
    dm.add_argument("--to", help=tr("보관할 모듈 이름 (base/… 또는 domain/…)"))
    dm.add_argument("--attach", nargs="*", default=[], help=tr("연결할 레포 경로"))
    dm.add_argument("--dry-run", action="store_true")
    rc_ = sub.add_parser("recall", help=tr("보관된 스킬을 레포에 연결하거나 전역으로 되돌림"))
    rc_.add_argument("item", help=tr("skill:<이름>"))
    rc_.add_argument("--global", dest="to_global", action="store_true")
    rc_.add_argument("--attach", nargs="*", default=[])
    rc_.add_argument("--dry-run", action="store_true")
    for nm in ("attach", "detach"):
        x = sub.add_parser(nm, help=tr("레포 harness.yaml 에 모듈 연결") if nm == "attach" else tr("레포 harness.yaml 에 모듈 해제"))
        x.add_argument("module")
        x.add_argument("repo", nargs="?", default=".")
    sc = sub.add_parser("scan", help=tr("레포별 모듈 사용 현황(텍스트)"))
    sc.add_argument("--root", default=os.environ.get("HARNIST_ROOT", "~/Documents/github"))
    vp = sub.add_parser("view", help=tr("로컬 웹 지도"))
    vp.add_argument("--root", default=os.environ.get("HARNIST_ROOT", "~/Documents/github"))
    vp.add_argument("--port", type=int, default=8765)
    vp.add_argument("--manifest", action="append", default=[])
    vp.add_argument("--open", action="store_true")
    vp.add_argument("--no-baseline", action="store_true", help=tr("첫 실행 자동 기준선 측정을 끈다"))
    sk = sub.add_parser("skills", help=tr("모델이 받는 스킬 목록 — 출처·설명 길이·실사용·오버라이드"))
    sk.add_argument("--name-only", nargs="+", metavar="SKILL", help=tr("설명만 숨긴다 (이름은 남아 부를 수 있다)"))
    sk.add_argument("--reset", nargs="+", metavar="SKILL", help=tr("오버라이드를 지운다"))
    dp = sub.add_parser("demo", help=tr("가짜 데이터로 대시보드 띄우기 (읽기 전용, 실제 설정은 건드리지 않음)"))
    dp.add_argument("--port", type=int, default=8766)
    dp.add_argument("--open", action="store_true")
    dp.add_argument("--dir", help=tr("데모 세계를 만들 폴더 (기본: 임시 폴더)"))
    bp = sub.add_parser("bench", help=tr("세션 기동 시간·기본 컨텍스트 토큰·비용 측정 (claude -p 두 번)"))
    bp.add_argument("--label", default="check")
    bp.add_argument("--root", default=os.environ.get("HARNIST_ROOT", "~/Documents/github"))
    a = ap.parse_args(argv)

    try:
        if a.cmd == "init":
            print(tr("생성: {path}", path=init_manifest(Path(a.dir), a.modules)))
            return 0
        if a.cmd == "promote":
            print(tr("승격: {name} → {path}", name=a.name, path=tilde(promote(Path(a.manifest), a.name, a.to))))
            print(tr("이제 generate 로 CLAUDE.md 블록과 락을 갱신한다."))
            return 0
        if a.cmd == "audit":
            if a.mark:
                print(tr("점검 완료로 기록: {path}", path=tilde(mark_audit())))
                return 0
            new = new_since_audit()
            print(tr("점검 기록 없음 — 첫 점검이 필요하다") if new is None else
                  tr("마지막 점검 이후 새 전역 항목: {items}", items=", ".join(new)) if new else
                  tr("마지막 점검 이후 새 전역 항목 없음"))
            return 0
        if a.cmd == "usage":
            rows = usage_report(a.days)
            if a.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            print(tr("최근 {days}일 세션 기록 기준. 상시 = 모든 세션에 항상 올라가는 설명 글자 수", days=a.days))
            for v in ("미사용", "제한", "호출 기록 없음", "공통", "보관됨", "상시 실행"):
                group = [r for r in rows if r["verdict"] == v]
                if not group:
                    continue
                print(f"== {verdict_label(v)} ({len(group)})")
                for r in sorted(group, key=lambda r: (-r["always_chars"], r["name"])):
                    where = ", ".join(f"{Path(k).name}×{c}" for k, c in list(r["repos"].items())[:3])
                    mod = f" [{r['module']}]" if r["module"] else ""
                    print(tr("  {kind:6} {name:34} 세션 {sessions:3}  상시 {chars:4}자  {last:10}  {where}{mod}",
                             kind=r["kind"], name=r["name"], sessions=r["sessions"], chars=r["always_chars"],
                             last=r["last"] or "-", where=where, mod=mod))
            return 0
        if a.cmd == "demote":
            for line in demote(a.item, a.to, [Path(p) for p in a.attach], a.dry_run):
                print(line)
            if a.dry_run:
                print(tr("(dry-run) 아무것도 바꾸지 않았다"))
            return 0
        if a.cmd == "recall":
            if not a.to_global and not a.attach:
                raise HarnistError(tr("--global 또는 --attach <레포> 중 하나는 정한다"))
            for line in recall(a.item, a.to_global, [Path(p) for p in a.attach], a.dry_run):
                print(line)
            return 0
        if a.cmd in ("attach", "detach"):
            mp = (attach if a.cmd == "attach" else detach)(Path(a.repo), a.module)
            print(tr("연결: {module} — {path}. 이제 그 레포에서 generate 한다.", module=a.module, path=tilde(mp)) if a.cmd == "attach"
                  else tr("해제: {module} — {path}. 이제 그 레포에서 generate 한다.", module=a.module, path=tilde(mp)))
            return 0
        if a.cmd == "skills":
            if a.name_only:
                set_skill_overrides(a.name_only, "name-only")
            if a.reset:
                set_skill_overrides(a.reset, None)
            rows = listing_report(90)
            if not rows:
                print(tr("스킬 목록 기록이 없다 — 먼저 harnist bench 를 한 번 돌린다"))
                return 0
            print(tr("모델이 받는 스킬 {n}개, 설명 {c}자 (가장 최근 측정 세션 기준)", n=len(rows), c=sum(r["chars"] for r in rows)))
            for r in rows:
                print(f"  {r['origin']:8} {r['name']:42} {r['chars']:5}  {tr('호출')} {r['calls']:3}  {r['override'] or ''}")
            recs = listing_recs(rows)
            if recs:
                print(tr("설명을 숨겨도 되는 미사용 스킬 {n}개 ({c}자): harnist skills --name-only {names}",
                         n=len(recs), c=sum(x["chars"] for x in recs), names=" ".join(x["name"] for x in recs)))
            return 0
        if a.cmd == "demo":
            sys.path.insert(0, str(HERE))
            import demo as _demo
            _demo.run(a.port, a.open, a.dir)
            return 0
        if a.cmd == "bench":
            out = run_bench(a.label, Path(a.root).expanduser().resolve())
            base = read_json(bench_dir() / "baseline.json")
            sv = savings(base, out)
            for k, v in sv["per_session"].items():
                print(tr("{profile}: 세션당 {tokens:+,} 토큰 · ${usd:+.4f}(토큰 기준) · 로컬 기동 {local:+}초 (기준선 대비 절감)",
                         profile=k, tokens=v["tokens"], usd=v["usd"], local=v["local_s"]))
                for w in sv["why"].get(k, []):
                    cs = "; ".join(tr("{kind} {x} {s}초", kind=c["kind"], x=c["x"], s=c["s"]) for c in w["causes"])
                    print(tr("  느려짐 {seg} +{d}초", seg=w["seg"], d=w["d"]) + (f" — {cs}" if cs else "")
                          + (tr(" (출력 {a}→{b} 토큰)", a=w["a"], b=w["b"]) if w["seg"] == "tail" else ""))
            for d, w in sv["windows"].items():
                print(tr("최근 {days}일 추정 절감: {tokens:,} 토큰 · ${usd:.2f} (세션 {sessions}, 서브에이전트 {spawns})",
                         days=d, tokens=w["tokens"], usd=w["usd"], sessions=w["sessions"], spawns=w["agent_spawns"]))
            return 0
        if a.cmd == "scan":
            extra = sorted((Path(__file__).resolve().parent / "manifests").glob("*/harness.yaml"))
            st = collect_state(Path(a.root).expanduser().resolve(), extra)
            print(tr("== 프로젝트"))
            for p in st["projects"]:
                print(f"{p['name']:20} {p['status']:8} {p['path']}")
                print(tr("{pad:20} 직접: {roots}", pad="", roots=", ".join(p["roots"]) or "-"))
                for i in p["issues"][:5]:
                    print(f"{'':20} ! {i}")
            print(tr("== 모듈 (계층 · 사용 레포 수 · 설명)"))
            for m in st["modules"]:
                print(f"{m['key']:36} {m['layer']:8} {len(m['used_by'])}  {m['description']}")
            return 0
        if a.cmd == "view":
            extra = [Path(p).expanduser() for p in a.manifest]
            extra += sorted((Path(__file__).resolve().parent / "manifests").glob("*/harness.yaml"))
            serve(Path(a.root).expanduser().resolve(), a.port, extra, a.open, not a.no_baseline)
            return 0
        if a.cmd == "list":
            dirs = [Path(p).expanduser() for p in (a.registry or [])] or [DEFAULT_REGISTRY]
            for m in load_registry(dirs).values():
                print(f"{m.name:32} {m.layer:8} {m.spec.get('description', '')}")
            return 0
        plan = build_plan(Path(a.manifest), Path(a.out).expanduser() if a.out else None)
        if a.cmd == "graph":
            print(graph(plan, a.format))
            return 0
        if a.cmd == "lock":
            write_lock(plan)
            print(tr("harness.lock 갱신 ({n} 항목)", n=len(plan.lock["modules"])))
            return 0
        ledger = read_ledger(plan)
        if a.cmd == "check":
            actions, conflicts = compute_actions(plan, ledger)
            ld = lock_diff(plan)
            pending = [x for x in actions if x[0] != "adopt"]
            for k, r in pending:
                print(tr("드리프트 {kind:6} {path}", kind=k, path=r))
            for c in conflicts:
                print(tr("충돌 {msg}", msg=c))
            for d in ld:
                print(tr("락 {diff}", diff=d))
            if pending or conflicts or ld:
                return 1
            print(tr("정합 — 생성물 {n}개 + 블록 {blocks}, 락 일치", n=len(plan.files), blocks=", ".join(plan.blocks)))
            return 0
        # generate
        ld = lock_diff(plan)
        if a.frozen and ld:
            print(tr("harness.lock 과 다름 (--frozen):"), *ld, sep="\n  ", file=sys.stderr)
            return 1
        actions, conflicts = compute_actions(plan, ledger, a.adopt, a.force)
        if conflicts:
            print(tr("중단 — 아무것도 쓰지 않았다:"), *conflicts, sep="\n  ", file=sys.stderr)
            return 1
        for k, r in actions:
            if k != "adopt":
                print(f"{k:6} {r}")
        if a.dry_run:
            print(tr("(dry-run) 변경 {n}건, 락 {lock}", n=len([x for x in actions if x[0] != "adopt"]),
                     lock=tr("변경 {n}건", n=len(ld)) if ld else tr("일치")))
            return 0
        apply(plan, actions, ledger)
        if ld:
            write_lock(plan)
            for d in ld:
                print(tr("락 {diff}", diff=d))
        n = len([x for x in actions if x[0] != "adopt"])
        print(tr("완료 — 변경 {n}건", n=n) if n else tr("변경 없음"))
        return 0
    except HarnistError as e:
        print(tr("오류: {error}", error=e), file=sys.stderr)
        return 2


# ---------------------------------------------------------------- i18n: English strings
# 키 = tr() 에 넘기는 한국어 템플릿 그대로, 값 = 영어 템플릿. 자리표시자 이름은 같게 둔다.
# 테스트가 harnist.py 의 모든 tr() 템플릿이 여기 있는지 확인한다.

EN = {
    "skillOverrides 갱신: {names} → {value}": "skillOverrides updated: {names} → {value}",
    "기본값": "default",
    "모델이 받는 스킬 목록 — 출처·설명 길이·실사용·오버라이드": "the skill list the model receives — origin, description length, real usage, overrides",
    "설명만 숨긴다 (이름은 남아 부를 수 있다)": "hide only the description (the name stays and can still be invoked)",
    "오버라이드를 지운다": "remove the override",
    "스킬 목록 기록이 없다 — 먼저 harnist bench 를 한 번 돌린다": "No skill list on record yet — run harnist bench once first",
    "모델이 받는 스킬 {n}개, 설명 {c}자 (가장 최근 측정 세션 기준)": "The model receives {n} skills, {c} description chars (latest measured session)",
    "호출": "calls",
    "설명을 숨겨도 되는 미사용 스킬 {n}개 ({c}자): harnist skills --name-only {names}": "{n} unused skills whose descriptions can be hidden ({c} chars): harnist skills --name-only {names}",
    # 파일·레지스트리·의존 해석
    "파일 없음: {path}": "File not found: {path}",
    "레지스트리 디렉터리 없음: {path}": "Registry directory not found: {path}",
    "{file}: name '{name}' 이 경로 '{path}' 과 다름": "{file}: name '{name}' does not match its path '{path}'",
    "{name}: 알 수 없는 layer '{layer}'": "{name}: unknown layer '{layer}'",
    "{name}: project 계층 모듈은 레포의 .harnist/modules 에만, 그 밖의 계층은 공유 레지스트리에만 둔다":
        "{name}: project layer modules belong only in the repo's .harnist/modules, other layers only in a shared registry",
    "모듈 중복: {name} ({a} / {b})": "Duplicate module: {name} ({a} / {b})",
    "모듈 없음: {name} (경로: {path})": "Module not found: {name} (path: {path})",
    "순환 의존: {path}": "Circular dependency: {path}",
    "계층 역전: {name}({layer}) 이 {dep}({dep_layer}) 에 의존 — 하위 계층은 상위 계층을 알 수 없다":
        "Layer inversion: {name} ({layer}) depends on {dep} ({dep_layer}) — a lower layer may not depend on a higher one",
    "settings 충돌 {key}: {a} 와 {b} 의 값이 다름": "settings conflict at {key}: {a} and {b} set different values",
    "{where}: frontmatter 가 없어 model 을 지정할 수 없음": "{where}: no frontmatter, cannot set model",
    # 플랜
    "알 수 없는 scope: {scope}": "Unknown scope: {scope}",
    "user 스코프는 매니페스트 target 이나 --out 으로 출력 위치를 명시해야 한다":
        "user scope needs an output location: set target in the manifest or pass --out",
    "출력 충돌: {path} 을 {a} 와 {b} 가 동시에 제공": "Output conflict: {path} is provided by both {a} and {b}",
    "{module}: 스킬 없음 {path}/SKILL.md": "{module}: skill not found {path}/SKILL.md",
    "{module}: 에이전트 없음 {path}": "{module}: agent not found {path}",
    "MCP 충돌: {name} 을 여러 모듈이 다르게 정의": "MCP conflict: modules define {name} differently",
    "spawn.routing: 에이전트 '{agent}' 를 제공하는 모듈이 없음": "spawn.routing: no module provides agent '{agent}'",
    "teams.{team}: 멤버 '{member}' 가 제공된 에이전트·스킬에 없음":
        "teams.{team}: member '{member}' is not a provided agent or skill",
    "user 스코프는 skills·agents·CLAUDE.md 블록만 관리한다 — 플러그인·settings·MCP 를 쓰는 모듈은 project 매니페스트에서 사용":
        "user scope manages only skills, agents and the CLAUDE.md block — use modules with plugins, settings or MCP in a project manifest",
    "mirror 는 레포 루트의 .md 파일 이름이어야 함: {file}": "mirror must be a .md file name at the repo root: {file}",
    "user 스코프는 mirror 를 지원하지 않는다 (각 에이전트의 전역 위치가 다름)":
        "user scope does not support mirror (each agent keeps its global files in a different place)",
    "links 는 refuse | skip | follow 중 하나: {value}": "links must be refuse, skip or follow: {value}",
    # CLAUDE.md 블록 (ko 는 원문 그대로 써야 기존 레포가 드리프트하지 않는다)
    "<!-- 생성물: harness.yaml 을 고치고 `harnist generate` 로 재생성한다. 직접 고치면 check 가 드리프트로 잡는다. -->":
        "<!-- Generated: edit harness.yaml and run `harnist generate`. Hand edits show up as drift in check. -->",
    "## 하네스 모듈": "## Harness modules",
    "| 모듈 | 계층 | 제공 |": "| Module | Layer | Provides |",
    "규칙만": "rules only",
    "### 팀": "### Teams",
    " · 리드 `{lead}`": " · lead `{lead}`",
    "**{team}** — {purpose}  \n구성: {members}{lead}": "**{team}** — {purpose}  \nMembers: {members}{lead}",
    "### 스폰 규칙": "### Spawn rules",
    "한 번에 동시에 띄우는 서브에이전트는 {n}개를 넘기지 않는다.": "Run at most {n} subagents at the same time.",
    "모델 라우팅(에이전트 frontmatter 에 반영됨): {routes}": "Model routing (written to agent frontmatter): {routes}",
    "### 이 레포 고유": "### Specific to this repo",
    # 적용·드리프트
    "이 출력 폴더는 다른 매니페스트가 관리한다: {path} (--force 로 넘겨받기)":
        "This output folder is managed by another manifest: {path} (--force to take over)",
    "심볼릭 링크 너머 경로: {path} — 다른 도구가 관리하는 곳 (매니페스트 links: skip 으로 건너뛰기)":
        "Path goes through a symlink: {path} — another tool manages it (set links: skip in the manifest to skip it)",
    "수동 수정됨: {path} (--force 로 덮어쓰기)": "Edited by hand: {path} (--force to overwrite)",
    "관리 밖 파일: {path} (--adopt 로 편입)": "Unmanaged file: {path} (--adopt to take ownership)",
    "수동 수정된 파일이 더 이상 생성되지 않음: {path} (--force 로 삭제)":
        "Hand-edited file is no longer generated: {path} (--force to delete)",
    "수동 수정됨: {file} 의 harnist 블록 (--force 로 덮어쓰기)": "Edited by hand: harnist block in {file} (--force to overwrite)",
    "관리 밖 harnist 블록: {file} (--adopt 로 편입)": "Unmanaged harnist block: {file} (--adopt to take ownership)",
    "수동 수정된 harnist 블록이 더 이상 생성되지 않음: {file} (--force 로 제거)":
        "Hand-edited harnist block is no longer generated: {file} (--force to remove)",
    "harness.lock 없음": "no harness.lock",
    "~ 마켓플레이스 {name}: {a} → {b}": "~ marketplace {name}: {a} → {b}",
    "~ {name} (내용 변경)": "~ {name} (content changed)",
    # init / promote / attach
    "이미 있음: {path}": "Already exists: {path}",
    "레지스트리에 없는 모듈: {names}": "Not in the registry: {names}",
    "# harnist 매니페스트 — 이 레포가 쓰는 하네스 모듈 선언. .claude/ 는 `harnist generate` 의 생성물이다.":
        "# harnist manifest — the harness modules this repo uses. .claude/ is generated by `harnist generate`.",
    "{name}: 이 레포의 project 모듈이 아님": "{name}: not a project module of this repo",
    "승격 대상은 base/… 또는 domain/… 이어야 함: {to}": "Promotion target must be base/… or domain/…: {to}",
    "이미 있는 모듈 이름: {name}": "Module name already exists: {name}",
    "project 모듈에 의존하고 있어 먼저 승격해야 함: {names}": "Depends on project modules, promote them first: {names}",
    "modules: 블록을 찾지 못해 자동 편집할 수 없음 — 직접 추가":
        "No modules: block found, cannot edit automatically — add it by hand",
    "{path}: 자동 편집 결과가 YAML 이 아님 — 파일은 그대로 두었다 ({error})":
        "{path}: the automatic edit is not valid YAML — file left unchanged ({error})",
    "{path}: 자동 편집 결과가 예상과 다름 — 파일은 그대로 두었다":
        "{path}: the automatic edit gave an unexpected result — file left unchanged",
    "{path}: {module} 이 직접 선언되어 있지 않음": "{path}: {module} is not declared directly",
    # 보관 + 스텁
    "{path} 에 스텁이 아닌 스킬이 있어 덮어쓰지 않는다": "{path} holds a skill that is not a stub, not overwriting it",
    "(보관됨) {desc}": "(archived) {desc}",
    "# {name} — harnist 레지스트리에 보관된 스킬": "# {name} — skill archived in the harnist registry",
    "{date} 전역 점검에서 사용 기록이 적어 전역에서 내리고 `{module}` 모듈로 보관했다. 사용자가 직접 불렀으니 그대로 수행한다.":
        "On {date} the global audit found little use, so this skill was removed from global and archived in the `{module}` module. The user called it directly, so carry it out as usual.",
    "1. `{dir}/SKILL.md` 를 읽고 그 절차를 따른다. 이 스킬의 기준 디렉터리는 `{dir}/` 이다. 본문에 `~/.claude/skills/{name}/` 경로가 나오면 이 위치로 바꿔 읽는다.":
        "1. Read `{dir}/SKILL.md` and follow its procedure. This skill's base directory is `{dir}/`. Where the text mentions `~/.claude/skills/{name}/`, read it as this location.",
    "2. 사용자가 함께 준 인자는 이 메시지의 ARGUMENTS 에 있다.": "2. Any arguments the user gave are in this message's ARGUMENTS.",
    "3. 작업이 끝나면 한 번만 묻는다. 이 레포에서 계속 쓰기(`harnist recall skill:{name} --attach .`), 전역으로 되돌리기(`harnist recall skill:{name} --global`), 지금처럼 두기 중 무엇을 원하는지. `harnist` 는 `python3 \"$(cat ~/.claude/.harnist/home)/harnist.py\"` 이다.":
        "3. When done, ask once whether to keep using it in this repo (`harnist recall skill:{name} --attach .`), restore it globally (`harnist recall skill:{name} --global`), or leave it as is. `harnist` means `python3 \"$(cat ~/.claude/.harnist/home)/harnist.py\"`.",
    "skill:{name} 은 harnist 스텁이 아님 (보관된 적 없거나 이미 복귀됨)":
        "skill:{name} is not a harnist stub (never archived, or already restored)",
    # recall / demote
    "recall 은 스텁이 남는 skill 만 받는다 — 에이전트·플러그인·MCP 는 harnist attach <모듈> <레포> 로 연결한다":
        "recall takes only skills, which leave a stub — connect agents, plugins and MCP with harnist attach <module> <repo>",
    "보관 모듈 {module} 이 레지스트리에 없음": "Archive module {module} is not in the registry",
    "연결: {repo} ← {module}": "Connect: {repo} ← {module}",
    "  ! 생성 중단 — 이 레포에서 /harnist:sync 로 마무리": "  ! generate stopped — finish with /harnist:sync in that repo",
    "전역 복귀: 스텁 제거 후 글로벌 매니페스트에 {module} 추가·재생성":
        "Restore globally: remove the stub, add {module} to the global manifest and regenerate",
    "글로벌 재생성 충돌: {conflicts}": "Global regenerate conflict: {conflicts}",
    "전역 복귀: {src} → ~/.claude/skills/{name} (스텁 교체, 보관본은 레지스트리에 남김)":
        "Restore globally: {src} → ~/.claude/skills/{name} (replaces the stub, the archived copy stays in the registry)",
    "내릴 수 없는 종류: {kind} (skill·agent·plugin·mcp·module) — 훅은 settings.json 에서 직접 옮긴다":
        "Cannot demote this kind: {kind} (skill, agent, plugin, mcp, module) — move hooks in settings.json by hand",
    "글로벌 매니페스트에 {name} 이 없음": "{name} is not in the global manifest",
    "--to base/… 또는 domain/… 으로 보관할 모듈 이름을 정한다": "Name the archive module with --to base/… or domain/…",
    "전역에 없는 항목: {item}": "Not a global item: {item}",
    "{item} 은 이미 {module} 로 보관된 스텁": "{item} is already a stub archived in {module}",
    "{item} 은 harnist 글로벌 모듈 {module} 의 일부 — module:{module} 로 내린다":
        "{item} is part of the harnist global module {module} — demote module:{module} instead",
    "{module} 은 외부 원본을 가리키는 모듈이라 내용을 더할 수 없음":
        "{module} points at an external source, cannot add to it",
    "MCP {name} 설정에 env/headers 가 있어 레지스트리(git)에 그대로 옮기지 않는다 — 값을 ${{환경변수}} 로 바꾼 설정으로 모듈을 직접 만들고 claude mcp remove 로 전역에서 뗀다":
        "MCP {name} config has env/headers, so it is not copied to the registry (git) as is — create the module by hand with values replaced by ${{ENV_VAR}}, then remove it from global with claude mcp remove",
    "비밀값으로 보이는 문자열이 있어 git 레지스트리로 복사하지 않는다 — 환경변수로 바꾼 뒤 다시 실행: {hits}":
        "Found strings that look like secrets, not copying to the git registry — replace them with environment variables and run again: {hits}",
    "{module} 에 이미 {name} 이 있음": "{module} already has {name}",
    "보관: {kind} {name} → 레지스트리 {module}": "Archive: {kind} {name} → registry {module}",
    "  ! 생성 중단 — 이 레포에서 /harnist:sync 로 마무리: {output}":
        "  ! generate stopped — finish with /harnist:sync in that repo: {output}",
    "전역 해제: 글로벌 매니페스트에서 {name} 제거 후 재생성": "Remove from global: drop {name} from the global manifest and regenerate",
    "스텁: ~/.claude/skills/{name} (/{name} 로 부르면 보관본을 읽어 수행, 상시 비용 0)":
        "Stub: ~/.claude/skills/{name} (/{name} runs the archived copy, zero always-on cost)",
    "전역 해제: claude mcp remove {name} -s user": "Remove from global: claude mcp remove {name} -s user",
    "  ! 해제 실패 — 직접 실행: claude mcp remove {name} -s user ({error})":
        "  ! remove failed — run it yourself: claude mcp remove {name} -s user ({error})",
    "전역 해제: claude plugin disable {name} --scope user": "Remove from global: claude plugin disable {name} --scope user",
    "  ! 비활성화 실패 — 직접 실행: claude plugin disable {name} --scope user ({error})":
        "  ! disable failed — run it yourself: claude plugin disable {name} --scope user ({error})",
    "전역 해제: {src} → {dst} (백업 후 제거)": "Remove from global: {src} → {dst} (backed up, then removed)",
    # 측정
    "claude CLI 를 PATH 에서 찾지 못함": "claude CLI not found on PATH",
    "{label} 프로브 실패: {output}": "{label} probe failed: {output}",
    "기본": "default",
    "정적 점검: 세션 기록에서 전역 항목 사용을 집계한다": "Static audit: counting global item usage in session history",
    "{name} 세션 프로브: claude -p ({model}) 실행 중": "{name} session probe: running claude -p ({model})",
    "기본 모델": "default model",
    "  기본 컨텍스트 {tokens:,} 토큰 · ${cost:.4f} · 총 {wall}초": "  base context {tokens:,} tokens · ${cost:.4f} · {wall}s total",
    "  실패: {error}": "  failed: {error}",
    "첫 측정이라 기준선으로 저장했다": "First measurement, saved as the baseline",
    "저장: {path}": "Saved: {path}",
    "건너뜀 {item}: {error}": "Skipped {item}: {error}",
    "점검 완료 기록을 갱신했다": "Updated the audit record",
    # 대시보드
    "연결: {module}": "Connected: {module}",
    "오류: {error}": "Error: {error}",
    "이 레포에서 이미 실행 중인 작업이 있다": "A job is already running for this repo",
    "같은 작업이 이미 실행 중이다": "The same job is already running",
    "오류로 끝남: {result}": "Ended with an error: {result}",
    "끝: {result}": "Done: {result}",
    "지원하는 터미널을 찾지 못함": "No supported terminal found",
    "지도 루트 밖이거나 없는 폴더: {path}": "Folder is outside the map root or does not exist: {path}",
    "없는 작업": "No such job",
    "적용할 추천안을 하나 이상 고른다": "Select at least one recommendation to apply",
    "재측정을 시작한다": "Starting re-measurement",
    "연결할 모듈을 하나 이상 고른다": "Select at least one module to connect",
    "프로젝트 목적을 적는다": "Describe the project's purpose",
    "잘못된 요청": "Bad request",
    "harnist view — {url}  (루트 {root}, 종료 Ctrl-C)": "harnist view — {url}  (root {root}, Ctrl-C to quit)",
    "기준선 측정이 없어 처음 한 번 자동으로 잰다 (claude -p 두 번, 점검 탭에서 진행 상황 확인)":
        "No baseline yet, measuring once now (claude -p twice, progress in the Audit tab)",
    # CLI 도움말
    "Tuist식 Claude Code 하네스 생성기": "Tuist-style Claude Code harness generator",
    "출력 루트 (기본: 매니페스트 디렉터리)": "output root (default: the manifest's directory)",
    "harness.lock 과 다르면 중단": "stop if harness.lock differs",
    "관리 밖 기존 파일을 덮어쓰고 편입": "overwrite existing unmanaged files and take ownership",
    "수동 수정된 생성물도 덮어쓰기": "overwrite generated files even if edited by hand",
    "빈 harness.yaml 생성": "create an empty harness.yaml",
    "project 모듈을 공유 레지스트리로 승격": "promote a project module to the shared registry",
    "전역 점검 스냅샷 — --mark 로 현재 상태를 점검 완료로 기록":
        "global audit snapshot — --mark records the current state as audited",
    "전역 스킬·에이전트·플러그인의 레포별 실제 사용(세션 기록)":
        "per-repo usage of global skills, agents and plugins (from session history)",
    "전역 항목을 레지스트리 모듈로 내리고 필요한 레포에만 연결":
        "archive a global item as a registry module and connect it only to repos that need it",
    "skill:<이름> | agent:<이름> | plugin:<id> | module:<계층/이름>": "skill:<name> | agent:<name> | plugin:<id> | module:<layer/name>",
    "보관할 모듈 이름 (base/… 또는 domain/…)": "archive module name (base/… or domain/…)",
    "연결할 레포 경로": "repo paths to connect",
    "보관된 스킬을 레포에 연결하거나 전역으로 되돌림": "connect an archived skill to repos or restore it globally",
    "skill:<이름>": "skill:<name>",
    "레포 harness.yaml 에 모듈 연결": "add a module to a repo's harness.yaml",
    "레포 harness.yaml 에 모듈 해제": "remove a module from a repo's harness.yaml",
    "레포별 모듈 사용 현황(텍스트)": "module usage per repo (text)",
    "로컬 웹 지도": "local web dashboard",
    "첫 실행 자동 기준선 측정을 끈다": "skip the automatic baseline measurement on first run",
    "가짜 데이터로 대시보드 띄우기 (읽기 전용, 실제 설정은 건드리지 않음)":
        "open the dashboard with fake data (read-only, your real setup is not touched)",
    "데모 세계를 만들 폴더 (기본: 임시 폴더)": "folder for the demo world (default: a temp folder)",
    "세션 기동 시간·기본 컨텍스트 토큰·비용 측정 (claude -p 두 번)":
        "measure session startup time, base context tokens and cost (claude -p twice)",
    # CLI 출력
    "생성: {path}": "Created: {path}",
    "승격: {name} → {path}": "Promoted: {name} → {path}",
    "이제 generate 로 CLAUDE.md 블록과 락을 갱신한다.": "Now run generate to update the CLAUDE.md block and the lock.",
    "점검 완료로 기록: {path}": "Recorded as audited: {path}",
    "점검 기록 없음 — 첫 점검이 필요하다": "No audit record — run a first audit",
    "마지막 점검 이후 새 전역 항목: {items}": "New global items since the last audit: {items}",
    "마지막 점검 이후 새 전역 항목 없음": "No new global items since the last audit",
    "최근 {days}일 세션 기록 기준. 상시 = 모든 세션에 항상 올라가는 설명 글자 수":
        "Based on the last {days} days of session history. always-on = description characters loaded into every session",
    "  {kind:6} {name:34} 세션 {sessions:3}  상시 {chars:4}자  {last:10}  {where}{mod}":
        "  {kind:6} {name:34} sessions {sessions:3}  always-on {chars:4} chars  {last:10}  {where}{mod}",
    "(dry-run) 아무것도 바꾸지 않았다": "(dry-run) nothing changed",
    "--global 또는 --attach <레포> 중 하나는 정한다": "Pass --global or --attach <repo>",
    "연결: {module} — {path}. 이제 그 레포에서 generate 한다.": "Attached: {module} — {path}. Now run generate in that repo.",
    "해제: {module} — {path}. 이제 그 레포에서 generate 한다.": "Detached: {module} — {path}. Now run generate in that repo.",
    "{profile}: 세션당 {tokens:+,} 토큰 · ${usd:+.4f}(토큰 기준) · 로컬 기동 {local:+}초 (기준선 대비 절감)":
        "{profile}: per session {tokens:+,} tokens · ${usd:+.4f} (token-based) · local startup {local:+}s (saved vs. baseline)",
    "{kind} {x} {s}초": "{kind} {x} {s}s",
    "  느려짐 {seg} +{d}초": "  slower {seg} +{d}s",
    " (출력 {a}→{b} 토큰)": " (output {a}→{b} tokens)",
    "최근 {days}일 추정 절감: {tokens:,} 토큰 · ${usd:.2f} (세션 {sessions}, 서브에이전트 {spawns})":
        "Estimated savings over the last {days} days: {tokens:,} tokens · ${usd:.2f} ({sessions} sessions, {spawns} subagents)",
    "== 프로젝트": "== Projects",
    "{pad:20} 직접: {roots}": "{pad:20} direct: {roots}",
    "== 모듈 (계층 · 사용 레포 수 · 설명)": "== Modules (layer · repos using it · description)",
    "harness.lock 갱신 ({n} 항목)": "harness.lock updated ({n} entries)",
    "드리프트 {kind:6} {path}": "drift {kind:6} {path}",
    "충돌 {msg}": "conflict {msg}",
    "락 {diff}": "lock {diff}",
    "정합 — 생성물 {n}개 + 블록 {blocks}, 락 일치": "In sync — {n} generated files + block {blocks}, lock matches",
    "harness.lock 과 다름 (--frozen):": "Differs from harness.lock (--frozen):",
    "중단 — 아무것도 쓰지 않았다:": "Stopped — nothing was written:",
    "(dry-run) 변경 {n}건, 락 {lock}": "(dry-run) {n} changes, lock {lock}",
    "변경 {n}건": "{n} changes",
    "일치": "matches",
    "완료 — 변경 {n}건": "Done — {n} changes",
    "변경 없음": "No changes",
    # demo.py
    "데모 세계: {path}": "demo world: {path}",
}


if __name__ == "__main__":
    sys.exit(main())
