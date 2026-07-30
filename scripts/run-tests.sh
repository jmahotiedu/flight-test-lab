#!/usr/bin/env bash
set -Eeuo pipefail

REPEAT="${1:-1}"
if ! [[ "$REPEAT" =~ ^[1-9][0-9]*$ ]]; then
  echo "Usage: $0 [positive-repeat-count]" >&2
  exit 2
fi

mkdir -p evidence/junit evidence/logs

for ((run = 1; run <= REPEAT; run++)); do
  echo "=== test run ${run}/${REPEAT} ==="
  python -m pytest -v \
    --junitxml="evidence/junit/test-results.xml" \
    --log-file="evidence/logs/pytest.log" \
    --log-file-level=DEBUG
done
