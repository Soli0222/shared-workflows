#!/usr/bin/env bash
# Extract the classification script out of chart-guard.yaml so the test harness
# exercises exactly the code the workflow runs.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
yq -r '
  .jobs.guard.steps[]
  | select(.name == "Classify the chart diff")
  | .run
' "${here}/../../.github/workflows/chart-guard.yaml" > "${here}/guard.sh"

echo "extracted $(wc -l < "${here}/guard.sh") lines to ${here}/guard.sh"
