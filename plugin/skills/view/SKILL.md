---
name: view
disable-model-invocation: true
description: harnist 지도를 로컬 웹으로 띄운다 — 모듈 × 프로젝트 매트릭스(공통·도메인·전용이 어느 레포에 들어가는지), 프로젝트별 계층 조립도, 드리프트 상태. "하네스 지도 보여줘", "harnist view", "어떤 레포가 뭘 쓰는지 보여줘", "하네스 도식화" 요청 시 사용.
---

# harnist view — 로컬 지도

서버를 백그라운드로 띄운다. Bash의 `run_in_background`를 쓴다.

```bash
H="${HARNIST_HOME:-$(cat ~/.claude/.harnist/home 2>/dev/null || echo ~/.claude/plugins/marketplaces/harnist)}"
python3 "$H/harnist.py" view --open
```

기본 주소는 `http://127.0.0.1:8765/`이고, `--root`(기본 `~/Documents/github`) 아래에서 `harness.yaml`을 6단계 깊이까지 찾는다. 포트가 이미 쓰이고 있으면 그 주소에 이미 떠 있는지 `curl -s localhost:8765/api/state`로 먼저 확인하고, 떠 있으면 주소만 알린다. 다른 서버가 쓰고 있으면 `--port`로 바꾼다.

처음 띄울 때 측정 기록이 없으면 기준선을 자동으로 한 번 잰다. claude -p를 두 번 실행해 소액의 비용이 든다. 점검 탭에서 원버튼 점검·추천안 적용·재측정을 할 수 있고, 지도 탭 오른쪽 패널에서 모듈 연결·전용 에이전트 설계·터미널 열기를 할 수 있다.

페이지는 열 때마다, 그리고 "다시 읽기"를 누를 때마다 디스크를 다시 읽는다. 그래서 `generate` 뒤에 서버를 재시작할 필요가 없다. 사용자에게는 주소와 함께 화면 읽는 법을 한 줄로 알린다. 채운 칸은 직접 선언, 빈 칸은 의존으로 따라온 모듈, 주황은 손볼 곳이다.
