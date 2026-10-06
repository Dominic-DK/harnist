"""SessionStart — 대부분의 세션에서는 아무것도 출력하지 않는다(상시 컨텍스트 0). 무인 실행에서는 아무것도 하지 않는다.

알리는 경우는 세 가지뿐이다.
  1) 현재 레포에 harness.yaml 이 있고 생성물이 어긋났을 때 (이때만 엔진을 부른다)
  2) 하루 한 번: 첫 점검 전이거나, 마지막 점검 이후 새 전역 항목이 생겼을 때
  3) 하네스 없는 새 프로젝트 폴더일 때
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CLAUDE = Path(os.environ.get("HARNIST_CLAUDE_HOME", "~/.claude")).expanduser()
STATE = CLAUDE / ".harnist"


def engine_home() -> Path:
    """HARNIST_HOME → 엔진이 실행 때마다 남기는 기록(~/.claude/.harnist/home) → 마켓플레이스 클론."""
    if os.environ.get("HARNIST_HOME"):
        return Path(os.environ["HARNIST_HOME"]).expanduser()
    try:
        return Path((STATE / "home").read_text().strip())
    except OSError:
        return Path("~/.claude/plugins/marketplaces/harnist").expanduser()  # GitHub 마켓플레이스로 설치한 경우의 클론 위치


ENGINE = engine_home() / "harnist.py"
ROOT = Path(os.environ.get("HARNIST_ROOT", "~/Documents/github")).expanduser().resolve()


def read_json(p):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def global_ids():
    """harnist.item_ids 와 같은 규칙을 yaml 없이 — 이름만 본다."""
    ids = {f"skill:{p.parent.name}" for p in (CLAUDE / "skills").glob("*/SKILL.md")}
    ids |= {f"agent:{p.stem}" for p in (CLAUDE / "agents").glob("*.md")}
    ids |= {f"plugin:{k}" for k, v in (read_json(CLAUDE / "settings.json").get("enabledPlugins") or {}).items() if v}
    ids |= {f"mcp:{k}" for k in (read_json(CLAUDE.parent / ".claude.json").get("mcpServers") or {})}
    return ids


def daily_global_note():
    stamp = STATE / "hook-day"
    today = time.strftime("%Y-%m-%d")
    try:
        if stamp.read_text().strip() == today:
            return None
    except OSError:
        pass
    STATE.mkdir(parents=True, exist_ok=True)
    stamp.write_text(today)
    snap = read_json(STATE / "audit.json")
    if not snap:
        return ("harnist 가 설치됐지만 전역 점검을 한 적이 없다. 사용자에게 /harnist:audit 으로 "
                "전역 스킬·플러그인·MCP 를 실사용 이력과 대조해 정리할 수 있다고 한 번 알린다.",
                "[harnist] 첫 전역 점검 전 — /harnist:audit")
    new = sorted(global_ids() - set(snap.get("items", [])))
    if not new:
        return None
    names = ", ".join(new[:6]) + (f" 외 {len(new) - 6}개" if len(new) > 6 else "")
    return (f"마지막 전역 점검({snap.get('at')}) 이후 전역에 새로 붙은 항목: {names}. 모든 세션에 상시 로드된다. "
            "사용자에게 알리고, 특정 레포에서만 쓸 것이면 /harnist:audit 으로 레지스트리에 보관하고 그 레포에만 연결하자고 제안한다.",
            f"[harnist] 새 전역 항목 {len(new)}개 — /harnist:audit")


def repo_note(cwd):
    manifest = cwd / "harness.yaml"
    if manifest.is_file() and ENGINE.is_file():
        try:
            r = subprocess.run([sys.executable, str(ENGINE), "check", "-m", str(manifest)],
                               capture_output=True, text=True, timeout=12)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode == 0:
            return None
        lines = (r.stdout + r.stderr).strip().splitlines()
        head = "; ".join(lines[:6]) + (f" 외 {len(lines) - 6}건" if len(lines) > 6 else "")
        if r.returncode == 2:  # 매니페스트를 해석하지 못함 — 드리프트가 아니다
            return (f"이 레포 harness.yaml 을 해석하지 못했다: {head}. 사용자에게 알린다.",
                    "[harnist] harness.yaml 오류 — /harnist:sync 로 확인")
        return (f"이 레포 하네스가 매니페스트·원본과 어긋나 있다: {head}. 사용자에게 알리고, 원하면 /harnist:sync 로 재생성한다.",
                f"[harnist] 하네스 드리프트 {len(lines)}건 — /harnist:sync")
    if ROOT not in [cwd, *cwd.parents] or cwd == ROOT:
        return None
    if any((cwd / m).exists() for m in (".claude", ".git", "CLAUDE.md", "AGENTS.md")):
        return None  # 이미 시작된 프로젝트 — 새 폴더 안내는 빈 폴더에만
    try:
        visible = [p for p in cwd.iterdir() if not p.name.startswith(".")]
    except OSError:
        return None
    if len(visible) <= 5:
        return ("하네스가 없는 새 프로젝트 폴더다. 사용자가 작업 방향을 말하면 /harnist:init 으로 "
                "실사용 이력 기반의 스킬·플러그인·MCP 연결과 전용 에이전트 설계를 제안할 수 있다.",
                "[harnist] 새 폴더 — /harnist:init")
    return None


def unattended():
    """claude -p · Agent SDK · 자동화 실행 — 안내를 읽을 사람이 없고, 주입하면 자동화 결과가 흔들린다."""
    if os.environ.get("HARNIST_HOOK_FORCE") == "1":  # 무인 실행에서도 안내를 받고 싶을 때
        return False
    return (os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "0"
            or os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli").startswith("sdk"))


def main():
    if unattended():
        return  # 날짜 도장도 건드리지 않는다 — 다음 사람 세션이 안내를 받는다
    try:
        cwd = Path(json.load(sys.stdin).get("cwd") or os.getcwd()).resolve()
    except Exception:
        cwd = Path.cwd().resolve()
    notes = []
    for fn in (lambda: repo_note(cwd), daily_global_note):
        try:
            n = fn()
        except Exception:
            n = None  # 훅은 어떤 경우에도 세션을 방해하지 않는다
        if n:
            notes.append(n)
    if notes:
        print(json.dumps({
            "systemMessage": " · ".join(u for _, u in notes),
            "hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": "[harnist] " + " ".join(c for c, _ in notes)},
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
