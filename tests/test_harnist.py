import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# 기존 테스트는 한국어 메시지를 확인한다 — 하위 프로세스 테스트도 os.environ 을 물려받는다
os.environ["HARNIST_LANG"] = "ko"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harnist  # noqa: E402


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip())


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src-repo" / ".claude"
        write(self.src / "skills/alpha/SKILL.md", "---\nname: alpha\n---\n실행: `.claude/skills/alpha/run.sh`, 데이터 `data/x`\n")
        write(self.src / "skills/alpha/run.sh", "echo .claude/skills/alpha\n")
        write(self.src / "agents/worker.md", "---\nname: worker\nmodel: opus\n---\n본문\n")
        reg = self.tmp / "registry"
        write(reg / "base/core/module.yaml", """
            layer: base
            plugins:
              - id: p@mk
                marketplace: {source: github, repo: o/mk}
            settings: {permissions: {allow: [Read]}}
            claude_md: 공통 규칙
            """)
        write(reg / "domain/alpha/module.yaml", f"""
            layer: domain
            requires: [base/core]
            source: {self.src}
            skills: [alpha]
            agents: [worker]
            rewrites:
              - pattern: '(?<![\\w~/.-])data/'
                replace: '${{source_repo}}/data/'
            mcp: {{srv: {{type: http, url: http://x}}}}
            """)
        self.repo = self.tmp / "repo"
        self.man = self.repo / "harness.yaml"
        self.manifest({"modules": ["domain/alpha"]})
        os.environ["HARNIST_PLUGIN_HOME"] = str(self.tmp / "no-plugins")
        os.environ.pop("HARNIST_REGISTRY", None)

    def manifest(self, extra: dict):
        data = {"registries": [str(self.tmp / "registry")], **extra}
        self.man.parent.mkdir(parents=True, exist_ok=True)
        self.man.write_text(json.dumps(data))  # YAML 은 JSON 의 상위집합

    def run_cli(self, *args) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = harnist.main([args[0], "-m", str(self.man), *args[1:]])
        return rc, buf.getvalue()


class ResolveTest(Fixture):
    def test_dependency_order_and_outputs(self):
        rc, _ = self.run_cli("generate")
        self.assertEqual(rc, 0)
        settings = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertTrue(settings["enabledPlugins"]["p@mk"])
        self.assertEqual(settings["extraKnownMarketplaces"]["mk"]["source"]["repo"], "o/mk")
        self.assertIn("srv", json.loads((self.repo / ".mcp.json").read_text())["mcpServers"])
        md = (self.repo / "CLAUDE.md").read_text()
        self.assertLess(md.index("base/core"), md.index("domain/alpha"))

    def test_rewrites_scoped_by_file_glob(self):
        self.run_cli("generate")
        skill = (self.repo / ".claude/skills/alpha/SKILL.md").read_text()
        self.assertIn(f"{self.src.parent}/data/x", skill)
        self.assertIn("`.claude/skills/alpha/run.sh`", skill)  # project 스코프는 상대 경로 유지
        self.assertEqual((self.repo / ".claude/skills/alpha/run.sh").read_text(), "echo .claude/skills/alpha\n")

    def test_layer_inversion_rejected(self):
        p = self.tmp / "registry/base/core/module.yaml"
        p.write_text(p.read_text() + "requires: [domain/alpha]\n")
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 2)
        self.assertIn("계층 역전", out)

    def test_cycle_rejected(self):
        write(self.tmp / "registry/domain/beta/module.yaml", "requires: [domain/gamma]\n")
        write(self.tmp / "registry/domain/gamma/module.yaml", "requires: [domain/beta]\n")
        self.manifest({"modules": ["domain/beta"]})
        rc, out = self.run_cli("generate")
        self.assertIn("순환 의존", out)

    def test_duplicate_provider_rejected(self):
        write(self.tmp / "registry/domain/dup/module.yaml", f"source: {self.src}\nskills: [alpha]\n")
        self.manifest({"modules": ["domain/alpha", "domain/dup"]})
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 2)
        self.assertIn("출력 충돌", out)

    def test_routing_and_unknown_team_member(self):
        self.manifest({"modules": ["domain/alpha"], "spawn": {"routing": {"worker": "haiku"}}})
        self.run_cli("generate")
        self.assertIn("model: haiku", (self.repo / ".claude/agents/worker.md").read_text())
        self.manifest({"modules": ["domain/alpha"], "teams": {"t": {"members": ["ghost"]}}})
        rc, out = self.run_cli("generate")
        self.assertIn("ghost", out)

    def test_user_scope_requires_out_and_rejects_settings(self):
        self.manifest({"scope": "user", "modules": ["domain/alpha"]})
        rc, out = self.run_cli("generate")
        self.assertIn("--out", out)
        rc, out = self.run_cli("generate", "--out", str(self.tmp / "home"))
        self.assertIn("user 스코프는", out)


class DriftTest(Fixture):
    def setUp(self):
        super().setUp()
        (self.repo / "CLAUDE.md").write_text("# 사람이 쓴 머리말\n")
        rc, _ = self.run_cli("generate")
        assert rc == 0

    def test_idempotent_and_check_clean(self):
        rc, out = self.run_cli("generate")
        self.assertIn("변경 없음", out)
        rc, out = self.run_cli("check")
        self.assertEqual(rc, 0, out)

    def test_human_text_outside_block_preserved(self):
        self.assertTrue((self.repo / "CLAUDE.md").read_text().startswith("# 사람이 쓴 머리말\n"))

    def test_hand_edit_detected_and_protected(self):
        f = self.repo / ".claude/agents/worker.md"
        f.write_text(f.read_text() + "손으로 고침\n")
        rc, out = self.run_cli("check")
        self.assertEqual(rc, 1)
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 1)
        self.assertIn("수동 수정됨", out)
        self.assertIn("손으로 고침", f.read_text())  # 아무것도 쓰지 않음
        rc, _ = self.run_cli("generate", "--force")
        self.assertNotIn("손으로 고침", f.read_text())

    def test_block_hand_edit_detected(self):
        md = self.repo / "CLAUDE.md"
        md.write_text(md.read_text().replace("공통 규칙", "바뀐 규칙"))
        rc, out = self.run_cli("generate")
        self.assertIn("CLAUDE.md", out)
        self.assertEqual(rc, 1)

    def test_unmanaged_file_needs_adopt(self):
        write(self.tmp / "registry/domain/extra/module.yaml", f"source: {self.src}\nagents: []\nskills: []\n")
        (self.repo / ".claude/agents/worker.md").unlink()
        ledger = json.loads((self.repo / ".claude/.harnist/ledger.json").read_text())
        ledger["files"].pop(".claude/agents/worker.md")
        (self.repo / ".claude/.harnist/ledger.json").write_text(json.dumps(ledger))
        (self.repo / ".claude/agents/worker.md").write_text("기존 수작업 파일\n")
        rc, out = self.run_cli("generate")
        self.assertIn("관리 밖 파일", out)
        rc, out = self.run_cli("generate", "--adopt")
        self.assertEqual(rc, 0)

    def test_removed_module_files_deleted(self):
        self.manifest({"modules": ["base/core"]})
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.repo / ".claude/skills/alpha").exists())
        self.assertFalse((self.repo / ".mcp.json").exists())
        self.assertTrue((self.repo / ".claude/settings.json").exists())

    def test_source_change_breaks_frozen_lock(self):
        (self.src / "skills/alpha/SKILL.md").write_text("---\nname: alpha\n---\n새 버전\n")
        rc, out = self.run_cli("generate", "--frozen")
        self.assertEqual(rc, 1)
        self.assertIn("domain/alpha", out)
        rc, out = self.run_cli("check")
        self.assertEqual(rc, 1)
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 0)
        rc, out = self.run_cli("check")
        self.assertEqual(rc, 0, out)


class LocalModuleTest(Fixture):
    def add_local(self, name="mine", extra=""):
        d = self.repo / ".harnist/modules/project" / name
        write(d / "module.yaml", f"layer: project\nrequires: [domain/alpha]\nagents: [{name}-agent]\nclaude_md: 전용 규칙\n{extra}")
        write(d / f"agents/{name}-agent.md", f"---\nname: {name}-agent\n---\n`.claude/skills/alpha/run.sh` 를 쓴다\n")
        self.manifest({"modules": ["domain/alpha", f"project/{name}"]})

    def test_local_module_generated(self):
        self.add_local()
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.repo / ".claude/agents/mine-agent.md").exists())

    def test_project_layer_only_in_local_registry(self):
        write(self.tmp / "registry/domain/bad/module.yaml", "layer: project\n")
        rc, out = self.run_cli("generate")
        self.assertIn("project 계층 모듈은", out)

    def test_init_validates_modules(self):
        d = self.tmp / "fresh"
        d.mkdir()
        os.environ["HARNIST_REGISTRY"] = str(self.tmp / "registry")
        try:
            with self.assertRaises(harnist.HarnistError):
                harnist.init_manifest(d, ["domain/nope"])
            mp = harnist.init_manifest(d, ["domain/alpha"])
            self.assertIn("domain/alpha", mp.read_text())
        finally:
            os.environ.pop("HARNIST_REGISTRY")

    def test_promote_moves_module_and_keeps_output(self):
        self.add_local()
        self.run_cli("generate")
        before = (self.repo / ".claude/agents/mine-agent.md").read_bytes()
        dest = harnist.promote(self.man, "project/mine", "domain/mine")
        self.assertTrue((dest / "agents/mine-agent.md").exists())
        self.assertFalse((self.repo / ".harnist/modules/project/mine").exists())
        self.assertIn("domain/mine", self.man.read_text())
        spec = harnist.load_yaml(dest / "module.yaml")
        self.assertEqual(spec["layer"], "domain")
        self.assertTrue(any("${install}" in r["replace"] for r in spec["rewrites"]))
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.repo / ".claude/agents/mine-agent.md").read_bytes(), before)  # project 스코프 출력 불변
        rc, out = self.run_cli("check")
        self.assertEqual(rc, 0, out)

    def test_promote_refuses_local_dependency(self):
        self.add_local("a")
        d = self.repo / ".harnist/modules/project/b"
        write(d / "module.yaml", "layer: project\nrequires: [project/a]\n")
        with self.assertRaises(harnist.HarnistError):
            harnist.promote(self.man, "project/b", "domain/b")

    def test_collect_state_marks_direct_and_status(self):
        self.add_local()
        self.run_cli("generate")
        st = harnist.collect_state(self.tmp)
        p = [x for x in st["projects"] if x["name"] == "repo"][0]
        self.assertEqual(p["status"], "clean")
        self.assertIn("repo:project/mine", p["roots"])
        self.assertIn("base/core", p["modules"])
        self.assertNotIn("base/core", p["roots"])


class MirrorTest(Fixture):
    def test_mirror_created_and_removed_keeping_human_text(self):
        (self.repo / "AGENTS.md").parent.mkdir(parents=True, exist_ok=True)
        (self.repo / "AGENTS.md").write_text("# codex 용 사람 메모\n")
        self.manifest({"modules": ["domain/alpha"], "mirror": ["AGENTS.md"]})
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 0, out)
        text = (self.repo / "AGENTS.md").read_text()
        self.assertTrue(text.startswith("# codex 용 사람 메모"))
        self.assertIn("공통 규칙", text)
        self.manifest({"modules": ["domain/alpha"]})
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "# codex 용 사람 메모\n")
        rc, out = self.run_cli("check")
        self.assertEqual(rc, 0, out)

    def test_mirror_only_file_deleted_when_unmirrored(self):
        self.manifest({"modules": ["domain/alpha"], "mirror": ["AGENTS.md"]})
        self.run_cli("generate")
        self.manifest({"modules": ["domain/alpha"]})
        self.run_cli("generate")
        self.assertFalse((self.repo / "AGENTS.md").exists())

    def test_bad_mirror_name_rejected(self):
        self.manifest({"modules": ["domain/alpha"], "mirror": ["../x.md"]})
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 2)


class GlobalFixture(Fixture):
    """가짜 ~/.claude 와 세션 기록."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home" / ".claude"
        os.environ["HARNIST_CLAUDE_HOME"] = str(self.home)
        write(self.home / "skills/solo/SKILL.md", "---\nname: solo\ndescription: 한 레포에서만 쓰는 스킬\n---\n`~/.claude/skills/solo/run.sh` 실행\n")
        write(self.home / "skills/solo/run.sh", "echo hi\n")
        write(self.home / "agents/idle.md", "---\nname: idle\ndescription: 아무도 안 부름\n---\n")
        write(self.home / "settings.json", json.dumps({"enabledPlugins": {"p@mk": True}}))
        (self.tmp / "home" / ".claude.json").write_text(json.dumps({"mcpServers": {"open": {"type": "http", "url": "http://x"}, "secret": {"type": "stdio", "command": "x", "env": {"TOKEN": "s"}}}}))
        self.target = self.tmp / "work" / "app"
        (self.target / ".git").mkdir(parents=True)
        lines = [
            {"type": "assistant", "cwd": str(self.target), "timestamp": "2026-10-01T00:00:00Z",
             "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "solo"}}]}},
            {"type": "assistant", "cwd": str(self.target / "sub"), "timestamp": "2026-10-02T00:00:00Z",
             "message": {"content": [{"type": "tool_use", "name": "mcp__open__query", "input": {}}]}},
            {"type": "user", "cwd": str(self.target), "timestamp": "2026-10-03T00:00:00Z",
             "message": {"content": "<command-name>/p:do</command-name>"}},
        ]
        write(self.home / "projects/x/s1.jsonl", "\n".join(json.dumps(l) for l in lines) + "\n")
        self.temp_backup = harnist.TEMP_PREFIXES
        harnist.TEMP_PREFIXES = ()  # 픽스처가 임시 폴더 안에 있으므로
        self.reg_backup = harnist.DEFAULT_REGISTRY
        harnist.DEFAULT_REGISTRY = self.tmp / "registry"

    def tearDown(self):
        harnist.DEFAULT_REGISTRY = self.reg_backup
        harnist.TEMP_PREFIXES = self.temp_backup
        os.environ.pop("HARNIST_CLAUDE_HOME", None)


class UsageDemoteTest(GlobalFixture):
    def test_usage_verdicts_and_repo_attribution(self):
        rows = {f"{r['kind']}:{r['name']}": r for r in harnist.usage_report(36500)}
        self.assertEqual(rows["skill:solo"]["verdict"], "제한")
        self.assertEqual(list(rows["skill:solo"]["repos"]), [harnist.tilde(self.target)])
        self.assertEqual(rows["agent:idle"]["verdict"], "미사용")
        self.assertEqual(rows["mcp:open"]["repos"], {harnist.tilde(self.target): 1})  # 하위 폴더도 같은 레포
        self.assertEqual(rows["plugin:p@mk"]["sessions"], 1)  # /p:do 슬래시 호출

    def test_audit_snapshot_detects_new_global_item(self):
        self.assertIsNone(harnist.new_since_audit())
        harnist.mark_audit()
        self.assertEqual(harnist.new_since_audit(), [])
        write(self.home / "skills/fresh/SKILL.md", "---\nname: fresh\ndescription: 새로 깔림\n---\n")
        self.assertEqual(harnist.new_since_audit(), ["skill:fresh"])

    def test_demote_skill_attach_and_backup(self):
        log = harnist.demote("skill:solo", "domain/solo", [self.target])
        stub = harnist.frontmatter(self.home / "skills/solo/SKILL.md")
        self.assertEqual(stub["harnist-stub"], "domain/solo")  # 원래 자리엔 상시 비용 0 의 스텁만
        self.assertTrue(stub["disable-model-invocation"])
        self.assertFalse((self.home / "skills/solo/run.sh").exists())
        self.assertTrue(any(".trash" in l for l in log))
        mod = self.tmp / "registry/domain/solo"
        self.assertTrue((mod / "skills/solo/run.sh").exists())
        out = (self.target / ".claude/skills/solo/SKILL.md").read_text()
        self.assertIn("`.claude/skills/solo/run.sh`", out)  # ~/.claude 경로가 레포 상대 경로로 재작성
        self.assertIn("domain/solo", (self.target / "harness.yaml").read_text())
        trash = list((Path(harnist.__file__).parent / ".trash").glob("*/skills/solo"))
        self.assertTrue(trash)
        for t in trash:
            shutil.rmtree(t.parent.parent)

    def test_demote_refuses_skill_with_token(self):
        write(self.home / "skills/leaky/SKILL.md", "---\nname: leaky\ndescription: x\n---\nexport T=pk_" + "a1" * 15 + "\n")
        with self.assertRaises(harnist.HarnistError):
            harnist.demote("skill:leaky", "domain/leaky", [], dry=True)

    def test_demote_refuses_mcp_with_secrets(self):
        with self.assertRaises(harnist.HarnistError):
            harnist.demote("mcp:secret", "domain/s", [], dry=True)

    def test_dry_run_changes_nothing(self):
        harnist.demote("agent:idle", "domain/idle", [self.target], dry=True)
        self.assertTrue((self.home / "agents/idle.md").exists())
        self.assertFalse((self.tmp / "registry/domain/idle").exists())
        self.assertFalse((self.target / "harness.yaml").exists())

    def test_attach_detach_roundtrip_preserves_comments(self):
        mp = self.target / "harness.yaml"
        mp.write_text(f"# 내 주석\nregistries: [{self.tmp / 'registry'}]\nmodules:\n  - domain/alpha   # 핵심\n")
        harnist.attach(self.target, "base/core")
        self.assertEqual(harnist.load_yaml(mp)["modules"], ["domain/alpha", "base/core"])
        harnist.detach(self.target, "base/core")
        self.assertEqual(mp.read_text().count("# 내 주석"), 1)
        self.assertIn("# 핵심", mp.read_text())
        harnist.detach(self.target, "domain/alpha")
        self.assertEqual(harnist.load_yaml(mp)["modules"], [])


class HookTest(GlobalFixture):
    def run_hook(self, cwd, **env):
        import subprocess
        e = {**os.environ, "HARNIST_CLAUDE_HOME": str(self.home), "HARNIST_ROOT": str(self.tmp), **env}
        r = subprocess.run([sys.executable, str(Path(harnist.__file__).parent / "plugin/hooks/session_start.py")],
                           input=json.dumps({"cwd": str(cwd)}), capture_output=True, text=True, env=e)
        return r.stdout

    def test_silent_and_stateless_when_unattended(self):
        out = self.run_hook(self.target, CLAUDE_CODE_ENTRYPOINT="sdk-cli", CLAUDE_CODE_SESSION_ATTENDED="0")
        self.assertEqual(out, "")
        self.assertFalse((self.home / ".harnist/hook-day").exists())

    def test_first_audit_notice_once_per_day(self):
        env = {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ATTENDED": "1"}
        self.assertIn("/harnist:audit", self.run_hook(self.target, **env))
        self.assertNotIn("/harnist:audit", self.run_hook(self.target, **env))

    def test_hook_ids_match_engine(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hook", Path(harnist.__file__).parent / "plugin/hooks/session_start.py")
        os.environ["HARNIST_CLAUDE_HOME"] = str(self.home)
        h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(h)
        self.assertEqual(h.global_ids(), set(harnist.item_ids(harnist.global_items())))


class RecallTest(GlobalFixture):
    def test_recall_attach_then_global(self):
        harnist.demote("skill:solo", "domain/solo", [])
        rows = {f"{r['kind']}:{r['name']}": r for r in harnist.usage_report(36500)}
        self.assertEqual(rows["skill:solo"]["verdict"], "보관됨")
        other = self.tmp / "work" / "other"
        other.mkdir(parents=True)
        harnist.recall("skill:solo", False, [other])
        self.assertTrue((other / ".claude/skills/solo/run.sh").exists())
        harnist.recall("skill:solo", True, [])
        self.assertEqual((self.home / "skills/solo/run.sh").read_text(), "echo hi\n")
        self.assertFalse(harnist.frontmatter(self.home / "skills/solo/SKILL.md").get("harnist-stub"))
        with self.assertRaises(harnist.HarnistError):
            harnist.recall("skill:solo", True, [])  # 더 이상 스텁이 아님
        for t in (Path(harnist.__file__).parent / ".trash").glob("*/skills/solo"):
            shutil.rmtree(t.parent.parent)

    def test_linked_global_skill_keeps_external_store(self):
        store = self.tmp / "agents-store" / "linked"
        write(store / "SKILL.md", "---\nname: linked\ndescription: 링크 스킬\n---\n본문\n")
        (self.home / "skills/linked").symlink_to(store)
        harnist.demote("skill:linked", "domain/linked", [])
        self.assertTrue((store / "SKILL.md").exists())  # 다른 에이전트용 원본은 그대로
        self.assertTrue((self.tmp / "registry/domain/linked/skills/linked/SKILL.md").exists())
        for t in (Path(harnist.__file__).parent / ".trash").glob("*/skills/linked"):
            shutil.rmtree(t.parent.parent)


class SafetyTest(Fixture):
    def test_symlinked_output_refused_or_skipped(self):
        ext = self.tmp / "external" / "alpha"
        write(ext / "SKILL.md", "남의 파일\n")
        (self.repo / ".claude/skills").mkdir(parents=True)
        (self.repo / ".claude/skills/alpha").symlink_to(ext)
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 1)
        self.assertIn("심볼릭 링크", out)
        self.assertEqual((ext / "SKILL.md").read_text(), "남의 파일\n")
        self.manifest({"modules": ["domain/alpha"], "links": "skip"})
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 0, out)
        self.assertEqual((ext / "SKILL.md").read_text(), "남의 파일\n")
        self.assertTrue((self.repo / ".claude/agents/worker.md").exists())

    def test_marker_mentioned_in_prose_is_not_a_block(self):
        md = self.repo / "CLAUDE.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("규칙: `<!-- harnist:begin -->` 표식은 손대지 않는다.\n중요한 사람 규칙\n")
        self.run_cli("generate")
        rc, out = self.run_cli("generate", "--force")
        self.assertIn("중요한 사람 규칙", md.read_text())
        rc, out = self.run_cli("check")
        self.assertEqual(rc, 0, out)

    def test_other_manifest_cannot_take_over_output(self):
        self.run_cli("generate")
        m2 = self.repo / "other.yaml"
        m2.write_text(json.dumps({"registries": [str(self.tmp / "registry")], "modules": ["base/core"]}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = harnist.main(["generate", "-m", str(m2)])
        self.assertEqual(rc, 1)
        self.assertIn("다른 매니페스트", buf.getvalue())
        self.assertTrue((self.repo / ".claude/skills/alpha/SKILL.md").exists())

    def test_yaml_edit_scoped_to_modules_block(self):
        mp = self.repo / "harness.yaml"
        mp.write_text(f"registries: [{self.tmp / 'registry'}]\nmodules:\n- domain/alpha\nteams:\n  t:\n    members:\n    - domain/alpha\n")
        harnist.attach(self.repo, "base/core")
        self.assertEqual(harnist.load_yaml(mp)["modules"], ["domain/alpha", "base/core"])
        harnist.detach(self.repo, "domain/alpha")
        y = harnist.load_yaml(mp)
        self.assertEqual(y["modules"], ["base/core"])
        self.assertEqual(y["teams"]["t"]["members"], ["domain/alpha"])  # 다른 블록은 그대로


class StubLinkTest(GlobalFixture):
    def test_stub_never_writes_through_link(self):
        store = self.tmp / "agents-store" / "shared"
        write(store / "SKILL.md", "---\nname: shared\ndescription: 공용\n---\n원본\n")
        (self.home / "skills/shared").symlink_to(store)
        harnist.write_stub("shared", "domain/x", store, "공용", "module")
        self.assertEqual((store / "SKILL.md").read_text(), "---\nname: shared\ndescription: 공용\n---\n원본\n")
        self.assertFalse((self.home / "skills/shared").is_symlink())
        self.assertTrue(harnist.frontmatter(self.home / "skills/shared/SKILL.md")["harnist-stub"])
        for t in (Path(harnist.__file__).parent / ".trash").glob("*/skills/shared"):
            t.unlink() if t.is_symlink() else shutil.rmtree(t)


class BenchTest(GlobalFixture):
    def test_parse_debug_timeline_and_counts(self):
        log = self.tmp / "d.log"
        log.write_text("\n".join([
            "2026-10-06T01:00:00.000Z [DEBUG] start",
            "2026-10-06T01:00:00.100Z [DEBUG] Registered 23 hooks from 14 plugins",
            '2026-10-06T01:00:00.300Z [DEBUG] MCP server "pencil": Successfully connected (transport: stdio) in 60ms',
            '2026-10-06T01:00:05.000Z [DEBUG] MCP server "x": version negotiation probe timed out on the http transport',
            "2026-10-06T01:00:01.000Z [WARN] Skill listing over budget: 58 skills, 22650 chars > 8000 budget",
            "2026-10-06T01:00:02.500Z [DEBUG] [API REQUEST] /v1/messages",
            "2026-10-06T01:00:03.000Z [DEBUG] Stream started - received first chunk",
            "2026-10-06T01:00:04.000Z [DEBUG] end",
        ]))
        d = harnist.parse_debug(log)
        self.assertEqual((d["skills_n"], d["skills_chars"], d["hooks_n"]), (58, 22650, 23))
        self.assertEqual(d["to_request_s"], 2.5)
        self.assertEqual(d["mcp_ok"], ["pencil"])
        self.assertEqual(d["mcp_fail"], ["x"])

    def test_savings_extrapolates_by_window(self):
        base = {"probes": {"user": {"ctx_tokens": 30000, "cost_usd": 0.20, "end_s": 9}, "agent": {"ctx_tokens": 20000, "cost_usd": 0.02, "end_s": 6}}}
        cur = {"probes": {"user": {"ctx_tokens": 25000, "cost_usd": 0.15, "end_s": 5}, "agent": {"ctx_tokens": 18000, "cost_usd": 0.015, "end_s": 4}},
               "sessions": {30: {"sessions": 100, "agent_spawns": 40}}}
        sv = harnist.savings(base, cur)
        self.assertEqual(sv["per_session"]["user"]["tokens"], 5000)
        self.assertEqual(sv["windows"]["30"]["tokens"], 5000 * 100 + 2000 * 40)
        # 금액은 토큰 감소분 × 기준선 토큰당 단가 (캐시로 흔들리는 실제 청구액 차이와 분리)
        self.assertAlmostEqual(sv["per_session"]["user"]["usd"], 5000 * 0.20 / 30000)
        self.assertAlmostEqual(sv["per_session"]["user"]["billed_usd"], 0.05)
        self.assertAlmostEqual(sv["windows"]["30"]["usd"], 5000 * 0.20 / 30000 * 100 + 2000 * 0.02 / 20000 * 40)

    def test_timeline_attributes_network_timeout_and_explains_slowdown(self):
        log = self.tmp / "slow.log"
        log.write_text("\n".join([
            "2026-10-06T01:00:00.000Z [DEBUG] start",
            "2026-10-06T01:00:00.200Z [DEBUG] Hooks: Registering",
            "2026-10-06T01:00:03.200Z [ERROR] Failed to fetch Grove settings: AxiosError: timeout of 3000ms exceeded",
            "2026-10-06T01:00:03.300Z [DEBUG] installPluginsForHeadless: starting",
            "2026-10-06T01:00:04.800Z [DEBUG] Total plugin output styles loaded: 0",
            "2026-10-06T01:00:05.000Z [DEBUG] [API REQUEST] /v1/messages",
            "2026-10-06T01:00:06.000Z [DEBUG] Stream started - received first chunk",
            "2026-10-06T01:00:07.000Z [DEBUG] end",
        ]))
        d = harnist.parse_debug(log)
        self.assertEqual(d["seg"]["net"], 3.0)
        self.assertEqual(d["seg"]["plugins"], 1.5)
        self.assertEqual(d["causes"][0]["kind"], "net_timeout")
        self.assertEqual(d["causes"][0]["x"], "Grove settings")
        base = {"to_request_s": 1.0, "first_token_s": 2.0, "end_s": 3.0, "seg": {"local": 1.0, "net": 0.0, "plugins": 0.0}, "out_tokens": 4}
        why = {w["seg"]: w for w in harnist.explain_change(base, {**d, "out_tokens": 4})}
        self.assertIn("net", why)
        self.assertEqual(why["net"]["causes"][0]["x"], "Grove settings")
        self.assertNotIn("local", why)  # 하네스가 좌우하는 로컬 기동은 늘지 않았다

    def test_recommendations_skip_stubs_secrets_and_attach_existing_repos(self):
        rows = {f"{r['kind']}:{r['name']}": r for r in harnist.usage_report(36500)}
        recs = {r["item"]: r for r in harnist.recommendations(list(rows.values()), self.tmp)}
        self.assertEqual(recs["skill:solo"]["attach"], [harnist.tilde(self.target)])
        self.assertTrue(recs["mcp:secret"]["blocked"])
        self.assertFalse(recs["mcp:secret"]["checked"])
        self.assertIn("agent:idle", recs)

    def test_session_counts_excludes_probe_sessions(self):
        write(self.home / "projects/-x-harnist-probe/p.jsonl", "{}\n")
        c = harnist.session_counts(36500)
        self.assertEqual(c["sessions"], 1)


class ServerTest(GlobalFixture):
    def setUp(self):
        super().setUp()
        import socket
        import subprocess
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            self.port = so.getsockname()[1]
        env = {**os.environ, "HARNIST_CLAUDE_HOME": str(self.home), "HARNIST_REGISTRY": str(self.tmp / "registry")}
        self.proc = subprocess.Popen([sys.executable, str(Path(harnist.__file__)), "view", "--root", str(self.tmp),
                                      "--port", str(self.port), "--no-baseline"], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.proc.stdout.readline()

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait()
        super().tearDown()

    def req(self, path, body=None, token=None, host=None):
        import urllib.request
        import urllib.error
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=None if body is None else json.dumps(body).encode(),
                                   method="GET" if body is None else "POST")
        if token:
            r.add_header("X-Harnist-Token", token)
        if host:
            r.add_header("Host", host)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def token(self):
        _, page = self.req("/")
        return re.search(r'const TOKEN = "([^"]+)"', page).group(1)

    def test_post_requires_page_token(self):
        self.assertEqual(self.req("/api/terminal", {"repo": str(self.target)})[0], 403)
        self.assertEqual(self.req("/api/terminal", {"repo": str(self.target)}, token="guess")[0], 403)

    def test_foreign_host_blocked(self):
        self.assertEqual(self.req("/api/state", host="evil.example")[0], 403)

    def test_repo_outside_root_rejected(self):
        code, body = self.req("/api/apply", {"repo": "/etc", "modules": ["base/core"]}, token=self.token())
        self.assertEqual(code, 400)
        self.assertIn("루트 밖", body)

    def test_apply_from_dashboard_creates_manifest(self):
        code, body = self.req("/api/apply", {"repo": str(self.target), "modules": ["domain/alpha"]}, token=self.token())
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["rc"], 0)
        self.assertTrue((self.target / ".claude/skills/alpha/SKILL.md").exists())


class I18nTest(Fixture):
    # i18n 도입 전 코드로 만든 render_block 출력 — ko 로케일에서 바이트 단위로 같아야 기존 레포가 드리프트하지 않는다
    GOLDEN_BLOCK = (
        '<!-- harnist:begin -->\n<!-- 생성물: harness.yaml 을 고치고 `harnist generate` 로 재생성한다. 직접 고치면 check 가 드리프트로 잡는다. -->\n## 하네스 모듈\n\n| 모듈 | 계층 | 제공 |\n| --- | --- | --- |\n| base/core | base | plugins 1, mcp 1 |\n| domain/alpha | domain | skills 2, agents 1 |\n| project/p | project | 규칙만 |\n\n### base/core\n\n공통 규칙\n\n### 팀\n\n**t1** — 목적  \n구성: w, s1 · 리드 `w`\n\n**t2** —   \n구성: s2\n\n### 스폰 규칙\n\n한 번에 동시에 띄우는 서브에이전트는 3개를 넘기지 않는다.\n모델 라우팅(에이전트 frontmatter 에 반영됨): w→haiku\n\n- 규칙 하나\n\n### 이 레포 고유\n\n로컬\n<!-- harnist:end -->'
    )

    def lang(self, value):
        old = os.environ.get("HARNIST_LANG")
        os.environ["HARNIST_LANG"] = value
        self.addCleanup(lambda: os.environ.__setitem__("HARNIST_LANG", old) if old is not None else os.environ.pop("HARNIST_LANG", None))

    def test_english_layer_inversion_message(self):
        self.lang("en")
        p = self.tmp / "registry/base/core/module.yaml"
        p.write_text(p.read_text() + "requires: [domain/alpha]\n")
        rc, out = self.run_cli("generate")
        self.assertEqual(rc, 2)
        self.assertIn("Layer inversion", out)
        self.assertIsNone(re.search(r"[\uac00-\ud7a3]", out), out)

    def test_ui_lang_reads_env_at_call_time(self):
        self.lang("ko_KR.UTF-8")
        self.assertEqual(harnist.ui_lang(), "ko")
        os.environ["HARNIST_LANG"] = "en_US.UTF-8"
        self.assertEqual(harnist.ui_lang(), "en")

    def test_missing_translation_falls_back_to_korean(self):
        self.lang("en")
        self.assertNotIn("번역 없는 {x} 문구", harnist.EN)
        self.assertEqual(harnist.tr("번역 없는 {x} 문구", x=1), "번역 없는 1 문구")
        self.assertEqual(harnist.tr("변경 없음"), "No changes")

    def test_every_tr_template_has_english(self):  # harnist.py·demo.py 의 tr("...") 을 소스에서 긁는다
        import ast
        here = Path(harnist.__file__).parent
        src = (here / "harnist.py").read_text() + (here / "demo.py").read_text()
        keys = [ast.literal_eval('"' + k + '"') for k in re.findall(r'(?<![\w.])tr\(\s*"((?:[^"\\]|\\.)*)"', src)]
        self.assertGreater(len(keys), 100)
        missing = sorted({k for k in keys if k not in harnist.EN})
        self.assertEqual(missing, [])
        for k in keys:  # 자리표시자가 같아야 한다
            fields = lambda t: sorted(f for _, f, _, _ in __import__("string").Formatter().parse(t) if f)
            self.assertEqual(fields(k), fields(harnist.EN[k]), k)

    def golden_inputs(self):
        M = harnist.Module
        mods = [M("base/core", Path("/x"), {"layer": "base", "plugins": [{"id": "p@mk"}], "mcp": {"a": {}}}),
                M("domain/alpha", Path("/x"), {"layer": "domain", "skills": ["s1", "s2"], "agents": ["w"]}),
                M("project/p", Path("/x"), {"layer": "project"})]
        spawn = {"max_parallel_agents": 3, "routing": {"w": "haiku"}, "rules": ["규칙 하나"]}
        teams = {"t1": {"purpose": "목적", "members": ["w", "s1"], "lead": "w"}, "t2": {"members": ["s2"]}}
        return mods, [(mods[0], "공통 규칙")], spawn, teams, "로컬"

    def test_render_block_korean_unchanged(self):
        self.lang("ko")
        self.assertEqual(harnist.render_block(*self.golden_inputs()), self.GOLDEN_BLOCK)

    def test_render_block_english(self):
        self.lang("en")
        out = harnist.render_block(*self.golden_inputs())
        self.assertIn("## Harness modules", out)
        self.assertIn("| project/p | project | rules only |", out)
        self.assertIn("Run at most 3 subagents at the same time.", out)


class DemoTest(unittest.TestCase):
    def test_demo_world_builds_without_touching_real_home(self):
        import demo
        tmp = Path(tempfile.mkdtemp())
        paths = demo.build(tmp)
        self.assertTrue((paths["workspace"] / "shop-web/harness.yaml").exists())
        self.assertTrue((paths["registry"] / "domain/web-frontend/module.yaml").exists())
        self.assertTrue(str(paths["home"]).startswith(str(tmp)))
        self.assertEqual(len(list((paths["home"] / "projects").glob("*/*.jsonl"))) > 5, True)


if __name__ == "__main__":
    unittest.main()
