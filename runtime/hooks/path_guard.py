#!/usr/bin/env python3
"""harnist 업그레이드: 선언형 불변 셸(immutable_paths) · 단일 가변 슬롯(mutable_slots) · 예제 격리(quarantine_examples) 가드.

기존 harnist(harnist.py + runtime/hooks/rules.ts)는 서브에이전트 역할별로
skills, mcp, memory, denyTools(도구 전체 차단)를 정밀하게 깎아 프롬프트 크기를 28~32% 줄인다.
그러나 작성자(author/builder) 에이전트는 Write/Edit 도구가 열려 있어야 하므로,
기존 denyTools만으로는 불변 런타임 셸(harness.js, build_deck.py, gate.conf) 변조나
격리된 예제 폴더(examples/**, demos/**) 직접 참조/복제를 막지 못한다.

본 모듈은 두 가지를 제공한다:
  1. merge_role_boundaries(): harnist.py의 merge_roles / runtime_config 확장 계약
     - immutable_paths : 합집합 병합 (어느 모듈이라도 불변으로 선언한 경로는 수정 금지)
     - mutable_slots   : 교집합(명시 시) 또는 허용 슬롯 목록 (지정되면 이 슬롯 밖의 쓰기 거부)
     - quarantine_paths: 합집합 병합 (빌더 역할에서 Read/Grep/복제 금지하여 예제 앵커링 차단)
  2. evaluate_pre_tool_use(): Claude Code / AGY PreToolUse 훅에서 경로 정규화(../ 우회 차단) 및
     셸 리다이렉션/cp 우회까지 검사하는 초경량(<0.2ms) 결정론 판정기.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write_file", "edit_file"}
READ_TOOLS = {"Read", "Grep", "Glob", "read_file", "grep_search"}
SHELL_TOOLS = {"Bash", "run_command"}

EXTENDED_ROLE_KEYS = {
    "model",
    "skills",
    "mcp",
    "memory",
    "deny_tools",
    "immutable_paths",
    "mutable_slots",
    "quarantine_paths",
}


def merge_extended_roles(dst: dict[str, dict], src: dict[str, dict] | None, origin: str) -> None:
    if src is None:
        return
    for role_name, rule in src.items():
        unknown = sorted(set(rule) - EXTENDED_ROLE_KEYS)
        if unknown:
            raise ValueError(f"{origin}: roles.{role_name} 알 수 없는 키 {unknown}")
        d = dst.setdefault(role_name, {})
        for union_key in ("skills", "mcp", "deny_tools", "immutable_paths", "quarantine_paths"):
            if union_key in rule:
                vals = rule[union_key]
                if not isinstance(vals, list) or not all(isinstance(x, str) for x in vals):
                    raise ValueError(f"{origin}: roles.{role_name}.{union_key} 는 문자열 리스트여야 함")
                d[union_key] = sorted(set(d.get(union_key, [])) | set(vals))
        if "mutable_slots" in rule:
            vals = rule["mutable_slots"]
            if not isinstance(vals, list) or not all(isinstance(x, str) for x in vals):
                raise ValueError(f"{origin}: roles.{role_name}.mutable_slots 는 문자열 리스트여야 함")
            if "mutable_slots" in d:
                # 여러 모듈이 동시에 가변 슬롯을 제한하면 둘 다 허용한 교집합(더 좁은 경계)을 채택
                d["mutable_slots"] = sorted(set(d["mutable_slots"]) & set(vals))
            else:
                d["mutable_slots"] = sorted(set(vals))
        if "memory" in rule:
            v, cur = rule["memory"], d.get("memory")
            if v is True or cur is True:
                d["memory"] = True
            elif isinstance(v, list) or isinstance(cur, list):
                d["memory"] = sorted(set(v if isinstance(v, list) else []) | set(cur if isinstance(cur, list) else []))
            else:
                d["memory"] = False
        if "model" in rule:
            if d.get("model") not in (None, rule["model"]):
                raise ValueError(f"roles.{role_name}.model 충돌: {d['model']} vs {rule['model']}")
            d["model"] = rule["model"]


def normalize_repo_rel(raw_path: str, repo_root: Path) -> str:
    """절대경로·상대경로·../ 순회를 모두 정규화해 repo_root 기준 POSIX 상대경로로 변환.
    repo_root 바깥 경로는 '../OUTSIDE/...' 접두어를 붙여 mutable_slots 통과를 원천 차단한다."""
    root = repo_root.resolve()
    p = Path(os.path.expanduser(raw_path))
    resolved = p.resolve() if p.is_absolute() else (root / p).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return f"../OUTSIDE/{resolved.as_posix().lstrip('/')}"


def glob_matches(patterns: list[str], rel_path: str) -> bool:
    for pat in patterns:
        norm_pat = pat.strip("/")
        if fnmatch.fnmatch(rel_path, norm_pat):
            return True
        # 'examples/**'가 'examples/foo.js'와 'examples/a/b.js' 모두 매칭되도록 처리
        if norm_pat.endswith("/**"):
            prefix = norm_pat[:-3]
            if rel_path == prefix or rel_path.startswith(prefix + "/"):
                return True
    return False


def extract_target_paths(tool: str, tool_input: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(write_paths, read_paths) 튜플 추출. Bash 명령의 리다이렉트·cp·cat도 정적 추출."""
    writes: list[str] = []
    reads: list[str] = []

    if tool in WRITE_TOOLS:
        for k in ("file_path", "path", "Path", "notebook_path"):
            if isinstance(tool_input.get(k), str):
                writes.append(tool_input[k])
    elif tool in READ_TOOLS:
        for k in ("file_path", "path", "Path", "SearchPath"):
            if isinstance(tool_input.get(k), str):
                reads.append(tool_input[k])
    elif tool in SHELL_TOOLS:
        cmd = str(tool_input.get("command") or tool_input.get("CommandLine") or "")
        # 1) 셸 출력 리다이렉션 (> file, >> file, tee file)
        for m in re.finditer(r"(?:>>?|(?:\btee\s+(?:-a\s+)?))\s*([^\s;|&\"']+)", cmd):
            target = m.group(1)
            if not target.startswith("/dev/"):
                writes.append(target)
        # 2) sed -i / cp / mv 대상 추출
        for m in re.finditer(r"\bsed\s+-i(?:\s+['\"][^'\"]*['\"])?\s+\S+\s+([^\s;|&\"']+)", cmd):
            writes.append(m.group(1))
        for m in re.finditer(r"\b(?:cp|mv)\s+(?:-[a-zA-Z]+\s+)*([^\s;|&\"']+)\s+([^\s;|&\"']+)", cmd):
            reads.append(m.group(1))
            writes.append(m.group(2))
        # 3) cat / head / tail / less 읽기 추출
        for m in re.finditer(r"\b(?:cat|head|tail|less)\s+(?:-[a-zA-Z0-9]+\s+)*([^\s;|&\"'>]+)", cmd):
            reads.append(m.group(1))

    return writes, reads


def evaluate_pre_tool_use(
    cfg: dict[str, Any],
    role_name: str,
    tool: str,
    tool_input: dict[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    roles = cfg.get("roles") or {}
    rule = roles.get(role_name) or roles.get("*") or {}

    # 1) 기존 denyTools 검사
    for pat in rule.get("denyTools") or rule.get("deny_tools") or []:
        if (pat.endswith("*") and tool.startswith(pat[:-1])) or pat == tool:
            return {"decision": "deny", "reason": f"harnist runtime: 역할 '{role_name}'은 도구 {tool} 사용이 금지됨"}

    writes, reads = extract_target_paths(tool, tool_input)
    immutable = rule.get("immutable_paths") or rule.get("immutablePaths") or []
    mutable = rule.get("mutable_slots") if "mutable_slots" in rule else rule.get("mutableSlots")
    quarantine = rule.get("quarantine_paths") or rule.get("quarantinePaths") or []

    # 2) 불변 셸(immutable_paths) 및 단일 가변 슬롯(mutable_slots) 검사
    for wp in writes:
        rel = normalize_repo_rel(wp, repo_root)
        if immutable and glob_matches(immutable, rel):
            return {
                "decision": "deny",
                "reason": f"harnist boundary: '{rel}'은 불변 런타임 셸(immutable_paths)이므로 수정할 수 없음",
            }
        if mutable is not None and not glob_matches(mutable, rel):
            return {
                "decision": "deny",
                "reason": f"harnist boundary: '{rel}'은 허용된 가변 슬롯({mutable}) 밖의 경로임",
            }

    # 3) 예제 격리(quarantine_paths) 읽기/복제 차단
    for rp in reads:
        rel = normalize_repo_rel(rp, repo_root)
        if quarantine and glob_matches(quarantine, rel):
            return {
                "decision": "deny",
                "reason": f"harnist quarantine: '{rel}'은 격리된 예제 경로(quarantine_paths)이므로 작성 중 열람/복제 금지",
            }

    return {"decision": "allow"}
