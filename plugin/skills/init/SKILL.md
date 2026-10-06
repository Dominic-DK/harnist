---
name: init
disable-model-invocation: true
description: 새 프로젝트 폴더(또는 하네스가 없는 레포)에 harnist 하네스를 세팅한다. 프로젝트를 읽고 대화해서 메인 시스템의 공통·도메인 모듈을 연결하고, 비어 있는 역할은 이 프로젝트 전용 에이전트·스킬을 새로 설계해 project 모듈로 만든 뒤 .claude/ 를 생성한다. "하네스 세팅해줘", "이 프로젝트 에이전트 구성해줘", "프로젝트 시작하자", "harnist init", "에이전트 팀 짜줘(이 레포)" 요청 시 사용. 이미 harness.yaml 이 있으면 모듈 추가·수정도 이 스킬로 한다.
---

# harnist init — 프로젝트 하네스 세팅

목표는 두 가지를 같이 하는 것이다. 이 프로젝트에 맞는 에이전트·스킬을 새로 설계하는 일, 그리고 그 결과를 혼자 떨어진 `.claude/`가 아니라 메인 시스템(공유 레지스트리)에 연결된 모듈로 남기는 일이다. 이미 있는 것은 연결하고, 없는 것만 만든다.

엔진은 플러그인이 아니라 harnist 레포에 있다. 모든 명령은 아래 형태로 부른다.

```bash
H="${HARNIST_HOME:-$(cat ~/.claude/.harnist/home 2>/dev/null || echo ~/.claude/plugins/marketplaces/harnist)}"
python3 "$H/harnist.py" <명령>
```

## 1. 현황 파악

폴더를 읽는다: README·문서, 코드 언어와 빌드 도구, `git log --oneline -20`, 기존 `.claude/`와 CLAUDE.md. `harness.yaml`이 이미 있으면 새로 만들지 말고 7단계(수정 모드)로 간다.

기존 `.claude/`에 손으로 만든 에이전트·스킬이 있으면 버리지 않는다. 6단계에서 project 모듈로 옮기고 `--adopt`로 편입할지를 사용자에게 묻는다.

## 2. 목적 확인

폴더가 비었거나 의도가 코드에서 드러나지 않으면 사용자에게 묻는다. 한 번에 한 질문씩, 많아야 세 번이다.

1. 이 프로젝트가 만드는 것과 결과물의 형태
2. 앞으로 반복될 작업(리서치, 구현, 리뷰, 배포, 문서화, 운영 등)
3. 다른 레포와 같이 쓰는 것(팀 규칙, 지식 베이스, 데이터 홈)

## 3. 메인 시스템 조회

```bash
python3 "$H/harnist.py" list   # 공유 레지스트리 모듈
python3 "$H/harnist.py" scan   # 다른 레포가 무엇을 쓰는지
```

실사용 이력도 근거로 쓴다. `usage --json`에는 항목마다 어느 레포에서 몇 번 쓰였는지가 들어 있다. 이번 프로젝트와 목적이 비슷한 레포에서 실제로 쓰인 스킬·플러그인·MCP는 강한 후보이고, 설치만 돼 있고 쓰인 적 없는 것은 넣지 않는다.

```bash
python3 "$H/harnist.py" usage --json > /tmp/harnist-usage.json   # 필요한 항목만 골라 읽는다
```

전역에서 내려 레지스트리에 보관된 모듈(점검으로 내린 것)도 `list`에 나온다. 이 프로젝트에 필요하면 연결한다. 비슷한 성격의 다른 레포가 쓰는 조합이 좋은 출발점이다. 고르는 기준은 이렇다.

- **base 모듈**: 레포 성격과 무관하게 같이 가는 것. 팀 공용 규칙·플러그인이 있다면 그 base 모듈.
- **domain 모듈**: 2단계의 반복 작업과 설명이 겹치는 것.
- 고른 모듈마다 근거를 한 줄씩 사용자에게 보인다. 애매하면 넣지 않는다. 나중에 추가하는 비용은 낮다.

## 4. 빈 역할 설계

선택한 모듈이 덮지 못하는 반복 작업만 새로 만든다. 이 단계가 harness 스킬과 같은 결이다. 프로젝트의 일을 역할로 쪼개되, 아래 기준으로 형태를 정한다.

| 형태 | 언제 | 예 |
| --- | --- | --- |
| 에이전트 | 별도 컨텍스트가 필요하거나, 병렬로 돌거나, 다른 모델이 맞는 일 | 국가별 분석 워커, 독립 리뷰어 |
| 스킬 | 순서·규율·도구 사용법이 있는 절차 | 배포 절차, 판정 방법론 |
| claude_md | 항상 지켜야 하는 짧은 규칙 | 산출물 위치, 금지 사항 |

처음에는 에이전트 3개, 스킬 3개 이하로 시작한다. 쓰면서 부족한 것을 더하는 쪽이 빗나간 것을 지우는 쪽보다 싸다. 같은 목적의 에이전트·스킬은 한 모듈로 묶는다. 보통 `project/<레포이름>` 하나면 충분하다.

## 5. project 모듈 작성

```
.harnist/modules/project/<name>/
  module.yaml
  agents/<agent>.md
  skills/<skill>/SKILL.md (+ scripts/, references/)
```

```yaml
# module.yaml
name: project/<name>
layer: project
description: 한 줄 — scan·view 에 그대로 보인다
requires: [domain/...]          # 기대는 공유 모듈이 있으면
agents: [<agent>]
skills: [<skill>]
claude_md: |
  트리거와 규칙. 무엇을 할 때 어느 에이전트·스킬을 쓰는지.
```

에이전트 파일은 frontmatter에 `name`, `description`(언제 부르는지가 드러나게), `model`을 둔다. 스킬의 `description`에는 사용자가 실제로 할 법한 트리거 문장을 넣는다. 스킬 안에서 자기 파일을 가리킬 때는 레포 루트 기준 `.claude/skills/<skill>/...`로 쓴다. 나중에 승격하면 이 경로가 설치 위치에 맞게 자동으로 재작성된다.

## 6. 매니페스트와 생성

```bash
python3 "$H/harnist.py" init . --modules base/... domain/...
```

만들어진 `harness.yaml`에 project 모듈을 추가하고, 필요하면 `spawn`(동시 상한·모델 라우팅·규칙), `teams`, `overrides.claude_md`(레포 고유 메모 파일)를 적는다. 형식은 `$H/README.md`의 매니페스트 절을 따른다.

```bash
python3 "$H/harnist.py" generate --dry-run   # 계획을 사용자에게 요약해서 보인다
python3 "$H/harnist.py" generate
python3 "$H/harnist.py" check
```

"관리 밖 파일" 충돌이 나면 덮어쓰지 말고 사용자에게 보인다. 그 내용이 project 모듈로 옮겨졌는지 확인한 뒤 `--adopt`로 실행한다.

## 7. 수정 모드 (harness.yaml 이 이미 있을 때)

모듈 추가·제거는 `harness.yaml`을 고치고, 에이전트·스킬 내용은 `.harnist/modules/project/...` 쪽을 고친다. 그다음 `generate`를 실행한다. 생성물인 `.claude/` 아래 파일을 직접 고치지 않는다.

## 8. 보고

마지막에 세 가지를 짧게 알린다.

1. 공통·도메인·전용으로 무엇이 들어갔는지
2. 새 에이전트와 플러그인은 세션을 다시 시작해야 로드된다는 점. 플러그인이 새로 들어갔으면 시작할 때 설치 제안이 뜬다.
3. 지도 확인은 `/harnist:view`, 전용 모듈이 다른 레포에서도 필요해지면 `/harnist:promote`
