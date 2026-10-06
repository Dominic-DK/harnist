---
name: sync
disable-model-invocation: true
description: harness.yaml 이 있는 레포의 하네스를 매니페스트·원본에 다시 맞춘다. 드리프트 원인을 설명하고, 손으로 고친 생성물이 있으면 그 내용을 원본으로 옮긴 뒤 재생성한다. "하네스 동기화", "harnist sync", "드리프트 고쳐줘", "하네스 재생성", 원본 스킬을 고친 뒤 "반영해줘", 글로벌(~/.claude) 하네스 갱신 요청 시 사용.
---

# harnist sync — 재생성

```bash
H="${HARNIST_HOME:-$(cat ~/.claude/.harnist/home 2>/dev/null || echo ~/.claude/plugins/marketplaces/harnist)}"
python3 "$H/harnist.py" check
```

`check`가 내놓는 줄의 종류별로 처리한다.

| 줄 | 뜻 | 처리 |
| --- | --- | --- |
| `드리프트 create/update/delete` | 매니페스트나 원본이 바뀌었다 | 그대로 `generate` |
| `락 ~ <모듈>` | 원본 레포의 스킬·에이전트가 바뀌었다 | 무엇이 바뀌었는지 원본 레포의 `git log`로 확인해서 알리고 `generate` |
| `락 ~ 마켓플레이스` | 플러그인 마켓플레이스가 업데이트됐다 | 알리고 `generate` (락만 갱신) |
| `충돌 수동 수정됨` | 생성물을 손으로 고쳤다 | 아래 절차 |
| `충돌 관리 밖 파일` | harnist가 만들지 않은 파일이 같은 경로에 있다 | 사용자에게 보이고, 동의를 받아 `--adopt` |

손으로 고친 생성물은 바로 덮어쓰지 않는다. `.claude/.harnist/ledger.json`에 기록된 생성 시점과 비교해 무엇이 바뀌었는지 사용자에게 보인다. 그 변경을 원본으로 옮긴다. project 모듈이면 `.harnist/modules/...`, domain 모듈이면 module.yaml의 `source`가 가리키는 원본 레포다. 옮긴 다음 `generate --force`를 실행한다.

```bash
python3 "$H/harnist.py" generate
python3 "$H/harnist.py" check
```

글로벌 계층(`~/.claude`)은 `-m "$H/manifests/global/harness.yaml"`로 같은 절차를 밟는다. 새 에이전트나 플러그인이 생겼으면 세션을 다시 시작해야 로드된다고 알린다.
