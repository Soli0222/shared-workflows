#!/usr/bin/env bash
# Build a scratch repo with a chart, then exercise chart-guard against each route.
set -euo pipefail

REPO="$(dirname "$0")/repo"
rm -rf "$REPO"
mkdir -p "$REPO/charts/testchart/templates"
cd "$REPO"
git init -q -b main

cat > charts/testchart/Chart.yaml <<'EOF'
apiVersion: v2
name: testchart
description: guard test
type: application
version: 1.2.3
appVersion: "0.9.0"
EOF

cat > charts/testchart/values.yaml <<'EOF'
image:
  repository: ghcr.io/example/app
  tag: "0.9.0"
valkey:
  image:
    repository: valkey/valkey
    tag: "9.1.0-alpine3.23"
replicaCount: 1
EOF

cat > charts/testchart/templates/deployment.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      app: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}
    spec:
      containers:
      - name: app
        image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
EOF

cat > charts/testchart/README.md <<'EOF'
# testchart
EOF

cat > charts/testchart/.helmignore <<'EOF'
README.md
.helmignore
EOF

git add -A
git -c user.email=t@e -c user.name=t commit -qm base
git tag base
