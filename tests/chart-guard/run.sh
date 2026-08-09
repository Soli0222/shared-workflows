#!/usr/bin/env bash
# usage: run.sh <case-name> <expect: pass|fail> -- commands applied on top of `base`
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$HERE/repo"
name="$1"; expect="$2"; shift 3

cd "$REPO"
git checkout -q base
git branch -qD pr 2>/dev/null || true
git checkout -q -b pr

"$@"

git add -A
git -c user.email=t@e -c user.name=t commit -qm "$name" --allow-empty

out=$(CHART_NAME_IN="${CHART_NAME_IN:-}" CHART_DIR_IN="${CHART_DIR_IN:-}" REPOSITORY="Soli0222/testchart" \
      BASE_SHA="$(git rev-parse base)" bash "$HERE/guard.sh" 2>&1)
rc=$?

got=pass; [[ $rc -eq 0 ]] || got=fail
if [[ "$got" == "$expect" ]]; then
  echo "PASS  $name (expected $expect)"
else
  echo "FAIL  $name (expected $expect, got $got)"
  sed 's/^/      /' <<< "$out"
  exit 1
fi
