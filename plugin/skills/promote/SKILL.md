---
name: promote
disable-model-invocation: true
description: 이 레포의 project 모듈(전용 에이전트·스킬)을 공유 레지스트리의 domain/base 모듈로 올려 다른 레포에서도 쓰게 한다. "이거 다른 프로젝트에서도 쓰자", "공통으로 올려줘", "글로벌로 승격", "harnist promote" 요청 시 사용.
---

# harnist promote — 전용 모듈을 공유로

```bash
H="${HARNIST_HOME:-$(cat ~/.claude/.harnist/home 2>/dev/null || echo ~/.claude/plugins/marketplaces/harnist)}"
```

## 1. 일반화 점검

승격 전에 모듈 내용을 읽고, 이 레포에만 맞는 것을 찾아 사용자에게 보인다.

- 레포 이름, 고유 경로, 이 프로젝트 도메인 용어가 박힌 에이전트·스킬 문장
- 레포 루트에 있다고 가정한 데이터 경로(`data/...`). 다른 레포에서는 그 경로가 없다. 원본 위치를 가리키는 `rewrites` 규칙이 필요하다. `$H/registry/domain/persona-research/module.yaml`이 예시다.
- 이 레포에만 해당하는 규칙. 승격하지 말고 레포의 `overrides.claude_md`로 남긴다.

## 2. 승격

이름은 계층과 목적이 드러나게 짓는다. 예: `domain/ios-release`, `base/commit-rules`.

```bash
python3 "$H/harnist.py" promote project/<name> --to domain/<new-name>
python3 "$H/harnist.py" generate
```

`promote`가 하는 일은 다음과 같다. project 모듈 의존이 남아 있으면 거부한다.

1. 모듈을 공유 레지스트리(`$H/registry`)로 옮긴다.
2. `.claude/(skills|agents)/` 경로 재작성 규칙을 추가한다.
3. 이 레포의 `harness.yaml`과 다른 project 모듈 안의 이름을 바꾼다.

## 3. 마무리

- 다른 레포에서 쓰려면 그 레포 `harness.yaml`의 `modules`에 새 이름을 추가하고 `generate`한다. 그 레포에서 `/harnist:init` 수정 모드로 해도 된다.
- 모든 레포에 들어가야 하는 것(글로벌)이면 `$H/manifests/global/harness.yaml`에 추가한다. 다만 user 스코프는 스킬·에이전트·CLAUDE.md 블록만 받는다.
- 모듈의 원본은 이제 harnist 레포에 있다. 사용자에게 harnist 레포 커밋이 필요하다고 알린다.
