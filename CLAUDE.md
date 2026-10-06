# harnist

Tuist식 Claude Code 하네스 모듈 관리 도구. 설계와 사용법은 README.md / README.ko.md.

- 엔진은 `harnist.py` 단일 파일, 의존성은 pyyaml 하나로 유지한다. 대시보드는 `web/index.html`(화면) + `web/i18n.js`(10개 언어 문구, 키는 모든 언어가 같아야 한다).
- 동작을 바꾸면 `python3 -m unittest tests.test_harnist` 로 확인한다. 드리프트·충돌 보호(아무것도 쓰지 않고 중단), 링크 너머 쓰기 금지, 대시보드 토큰 검사는 테스트로 고정된 계약이다.
- 화면을 바꾸면 `python3 harnist.py demo` 로 띄워 확인하고, README 캡처(`docs/img/`)는 데모 화면으로만 만든다 — 실제 레포·설정이 찍히면 안 된다.
- `registry/`, `manifests/`, `.trash/` 는 개인 데이터라 gitignore 다. 공개 레포에 올리지 않는다.
