#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
R="$HERE/run.sh"
C=charts/testchart
fails=0

t() { bash "$R" "$@" || fails=$((fails + 1)); }

# 経路 A: appVersion だけ / version 不変 -> pass
t "A: appVersion only, version unchanged" pass -- \
  bash -c "yq -i '.appVersion = \"1.0.0\"' $C/Chart.yaml"

# appVersion + patch は経路 B 扱い (contract 4: patch = イメージが変わっただけ) -> pass
t "A: appVersion + patch" pass -- \
  bash -c "yq -i '.appVersion = \"1.0.0\" | .version = \"1.2.4\"' $C/Chart.yaml"

# appVersion の変更に minor を要求するのは行き過ぎ -> fail
t "A: appVersion + minor" fail -- \
  bash -c "yq -i '.appVersion = \"1.0.0\" | .version = \"1.3.0\"' $C/Chart.yaml"

# release-please の extra-files は Chart.yaml を YAML ラウンドトリップで書き戻すので
# 長い description が折り畳みスカラーに整形し直される。値は同じなので経路 A -> pass
t "A: appVersion + description reflow" pass -- \
  bash -c "yq -i '.appVersion = \"1.0.0\"' $C/Chart.yaml; yq -i -P '.description = .description' $C/Chart.yaml; python3 -c \"
import pathlib
p = pathlib.Path('$C/Chart.yaml')
s = p.read_text()
p.write_text(s.replace('description: guard test', 'description: >-\n  guard\n  test'))
\""

# 経路 B: image tag + patch -> pass
t "B: image tag + patch" pass -- \
  bash -c "yq -i '.image.tag = \"1.0.0\"' $C/values.yaml; yq -i '.version = \"1.2.4\"' $C/Chart.yaml"

# 経路 B: third-party image tag + patch -> pass
t "B: third-party tag + patch" pass -- \
  bash -c "yq -i '.valkey.image.tag = \"9.2.0\"' $C/values.yaml; yq -i '.version = \"1.2.4\"' $C/Chart.yaml"

# 経路 B: image tag + appVersion + patch (sui 型) -> pass
t "B: tag + appVersion + patch" pass -- \
  bash -c "yq -i '.image.tag = \"1.0.0\"' $C/values.yaml; yq -i '.version = \"1.2.4\" | .appVersion = \"1.0.0\"' $C/Chart.yaml"

# 経路 B: image tag なのに version 据え置き -> fail
t "B: image tag, no bump" fail -- \
  bash -c "yq -i '.image.tag = \"1.0.0\"' $C/values.yaml"

# 経路 B: image tag なのに minor -> fail
t "B: image tag but minor" fail -- \
  bash -c "yq -i '.image.tag = \"1.0.0\"' $C/values.yaml; yq -i '.version = \"1.3.0\"' $C/Chart.yaml"

# repository の変更は tag だけではないので構造変更扱い -> patch では fail
t "C: image repository change with patch" fail -- \
  bash -c "yq -i '.image.repository = \"ghcr.io/example/other\"' $C/values.yaml; yq -i '.version = \"1.2.4\"' $C/Chart.yaml"

# 経路 C: templates 変更 + version 据え置き -> fail
t "C: templates changed, no bump" fail -- \
  bash -c "printf '  # comment\n' >> $C/templates/deployment.yaml"

# 経路 C: templates 変更 + patch -> fail (pke automerge を守る要)
t "C: templates changed, patch only" fail -- \
  bash -c "printf '  # comment\n' >> $C/templates/deployment.yaml; yq -i '.version = \"1.2.4\"' $C/Chart.yaml"

# 経路 C: templates 変更 + minor -> pass
t "C: templates changed, minor" pass -- \
  bash -c "printf '  # comment\n' >> $C/templates/deployment.yaml; yq -i '.version = \"1.3.0\"' $C/Chart.yaml"

# 経路 C: templates 変更 + major -> pass
t "C: templates changed, major" pass -- \
  bash -c "printf '  # comment\n' >> $C/templates/deployment.yaml; yq -i '.version = \"2.0.0\"' $C/Chart.yaml"

# values の非 image 変更 + patch -> fail
t "C: values non-image change, patch" fail -- \
  bash -c "yq -i '.replicaCount = 2' $C/values.yaml; yq -i '.version = \"1.2.4\"' $C/Chart.yaml"

# catch-all: 新しい梱包物 (crds/) + patch -> fail
t "catch-all: crds added, patch" fail -- \
  bash -c "mkdir -p $C/crds; printf 'apiVersion: v1\nkind: List\n' > $C/crds/x.yaml; yq -i '.version = \"1.2.4\"' $C/Chart.yaml"

# catch-all: 新しい梱包物 (crds/) + minor -> pass
t "catch-all: crds added, minor" pass -- \
  bash -c "mkdir -p $C/crds; printf 'apiVersion: v1\nkind: List\n' > $C/crds/x.yaml; yq -i '.version = \"1.3.0\"' $C/Chart.yaml"

# catch-all: Chart.yaml に dependencies を足した + patch -> fail
t "catch-all: dependencies added, patch" fail -- \
  bash -c "yq -i '.dependencies = [{\"name\":\"x\",\"version\":\"1.0.0\",\"repository\":\"https://example.com\"}] | .version = \"1.2.4\"' $C/Chart.yaml"

# .helmignore の変更は梱包内容そのものを変えるので構造変更 -> patch では fail
t ".helmignore changed, patch" fail -- \
  bash -c "printf 'extra.txt\n' >> $C/.helmignore; yq -i '.version = \"1.2.4\"' $C/Chart.yaml"

# README だけ (梱包されない) -> version 不問で pass
t "README only, no bump" pass -- \
  bash -c "printf 'more docs\n' >> $C/README.md"

# 梱包されないファイルの追加 -> pass
t "unpackaged file added" pass -- \
  bash -c "printf 'x\n' > $C/notes.txt; printf 'notes.txt\n' >> $C/.helmignore; yq -i '.version = \"1.3.0\"' $C/Chart.yaml"

# 梱包物の削除 + minor -> pass
t "template deleted, minor" pass -- \
  bash -c "rm $C/templates/deployment.yaml; yq -i '.version = \"1.3.0\"' $C/Chart.yaml"

# 梱包物の削除 + patch -> fail
t "template deleted, patch" fail -- \
  bash -c "rm $C/templates/deployment.yaml; yq -i '.version = \"1.2.4\"' $C/Chart.yaml"

# version が下がる -> fail
t "version downgrade" fail -- \
  bash -c "printf '  # comment\n' >> $C/templates/deployment.yaml; yq -i '.version = \"1.2.2\"' $C/Chart.yaml"

# chart 外の変更だけ -> pass
t "no chart change" pass -- \
  bash -c "printf 'x\n' > unrelated.txt"

echo
if [[ $fails -eq 0 ]]; then echo "all cases passed"; else echo "${fails} case(s) failed"; exit 1; fi
