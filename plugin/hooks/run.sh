#!/bin/sh
# SessionStart 런처 — OS 마다 파이썬 실행 파일 이름이 달라서(python3 / python / py) 있는 것을 쓴다. 없으면 조용히 끝낸다.
here=$(dirname "$0")
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
    exec "$py" "$here/session_start.py"
  fi
done
if command -v py >/dev/null 2>&1; then exec py -3 "$here/session_start.py"; fi
exit 0
