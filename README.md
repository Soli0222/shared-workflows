# shared-workflows

自作アプリのリリースパイプラインで共有する GitHub Actions の reusable workflow 置き場。

## なぜ共有するのか

対象アプリは 9 リポジトリあり、image build workflow はファイル名すら
(`docker.yaml` / `docker.yml` / `build-image.yaml` / `release.yml`) 揃っていなかった。
同じ内容を 9 箇所で手直しするのをやめ、実体をここに1つ置く。

## 共有するのは `workflow_call` だけ

reusable workflow が**呼ばれた場合にだけ** `github` context は呼び出し元アプリのものになる。
このリポジトリ内の workflow を直接 `schedule` / `workflow_dispatch` すると
`github.repository` と `GITHUB_TOKEN` は**このリポジトリ**を指し、アプリのソースにも
GHCR package にも chart にも切り替わらない。

したがって**ここに置く workflow は `on: workflow_call` のみ**とし、
起動経路 (push / schedule / dispatch / pull_request) は各アプリ側に置く。

呼び出しは `@vX.Y.Z` のバージョンタグで固定する。
検証済みのコミットに新しいタグを付けて公開し、公開済みのタグは移動しない。
呼び出し元のタグ更新は Renovate が PR にする。

## workflow

| workflow | 役割 |
|---|---|
| `publish-image.yaml` | multi-arch image を build して `ghcr.io` へ publish する |
| `publish-chart.yaml` | 呼び出し元の既定ブランチ上の chart を `oci://ghcr.io/<owner>/charts` へ収束させる (調停処理) |
| `chart-guard.yaml` | PR の chart 変更と version の上げ方が噛み合っているかを検査する |
| `weekly-merge.yaml` | release-please の pending PR を CI 成功を条件に squash merge する |
| `release-image.yaml` | version-check → `publish-image` → latest タグ前進をまとめる |
| `release-pipeline.yaml` | release-please → `release-image` → `publish-chart` のリリース DAG |
| `release-retry.yaml` | 既存リリース版数から sha を解決して image / chart publish をやり直す |
| `chart-release.yaml` | charts/** への push / 定期実行から appVersion-only 変化を捌いて `publish-chart` を呼ぶ |
| `goreleaser.yaml` | タグ push / dispatch から GoReleaser でバイナリを公開する |

### `publish-image.yaml`

```yaml
publish-image:
  needs: release-please
  if: needs.release-please.outputs.release-created == 'true'
  permissions:
    contents: read
    packages: write
  uses: Soli0222/shared-workflows/.github/workflows/publish-image.yaml@v1.0.1
  with:
    ref: ${{ needs.release-please.outputs.sha }}
    version: ${{ needs.release-please.outputs.version }}
```

タグは `version` input から明示的に作る。DAG では `push: main` から呼ばれるため
`github.ref` は `refs/heads/main` であり、`type=semver` のような ref 依存の指定では
`1.4.1` のようなタグが作られない。OCI label の `org.opencontainers.image.version` /
`.revision` も event context ではなく inputs から設定する。

**preflight** が既存 manifest の上書きを防ぐ。

| 状態 | 動作 |
|---|---|
| version タグが無い | build 開始 |
| version タグがあり、期待 platform と revision が一致 | no-op |
| version タグがあり、platform または revision が不一致 | fail (上書きしない) |

「Re-run all jobs」でも通常の publish は再実行される。同じ release SHA でもベースイメージの
更新などで別 digest が生成され得るため、retry 系だけでなく通常系にも適用する。

`verify` job は build した場合も no-op だった場合も必ず通る完了条件で、
`linux/amd64` と `linux/arm64` が揃っていることを確認する。

### `publish-chart.yaml`

**特定のアプリ release に対応する chart を作る処理ではない。**
image publish 成功を契機として、**呼び出し元の既定ブランチ上の最新 chart を OCI へ収束させる**処理である。
連続した release の中間 version は、後続 release に包含されて chart が個別に作られない
場合がある。

特定の push event の diff ではなく **Git の現状 + OCI の現状**から動作を決めるので、
run が欠落しても次回が拾う。

| 判定順 | Git と OCI の状態 | 動作 |
|---|---|---|
| 1 | OCI の最大 version が Git の version より新しい | **fail** (out-of-band publish か commit back の欠落) |
| 2 | Git の version が OCI に無い | Git の version を**そのまま** publish |
| 3 | OCI にあり、内容が一致 | no-op |
| 4 | OCI にあり、差分が `appVersion` **だけ** | 次の patch を採番 → commit → publish |
| 5 | OCI にあり、それ以外の内容が異なる | **fail** (`chart-guard` の抜けを検知する backstop) |

「OCI の最大 version > Git の version」と「Git の version が OCI に無い」は同時に
成立し得るため、**fail 判定を先に置く**。

内容比較に `helm package` の tgz バイト比較は使えない (gzip のタイムスタンプで再現性が無い)。
Git 側も `helm package` を通してから展開し、OCI から pull して展開したものと**中身**を比較する。

deploy する image は、values.yaml のキー名検索では漏れるため、
**デフォルト値で `helm template` した PodSpec の `containers` と `initContainers` から
抽出**する。条件付きで render されない image は `extra-images` input で補う。

image が無かった場合は **deferred success** で終える。GitHub Actions の step 終了コードは
`0 = success` / それ以外 = failure で neutral 用の終了コードが存在しないため、
`::warning::` を出して `exit 0` し、job output に `result=deferred` を返す。
**有限時間の待機はしない** — release A の chart job が B の image を待っている間、
B 自身の chart job は同じ concurrency group で後ろに詰まるため。

commit back が non-fast-forward で拒否されたら、`git pull --rebase` して**そのまま続行しない**。
rebase で appVersion や chart の内容が変わると、それ以前に行った image 検証と OCI 比較が
無効になる。**最初から判定をやり直す。**

```yaml
publish-chart:
  permissions:
    contents: write
    packages: write
  uses: Soli0222/shared-workflows/.github/workflows/publish-chart.yaml@v1.0.1
  with:
    api-versions: monitoring.coreos.com/v1
```

### `chart-guard.yaml`

`patch = イメージが変わっただけ` / `minor・major = 人間が chart の形を変えた` を
required check として強制する。pke 側はこれを信頼して自作 chart の patch を automerge する。
**約束ではなく check で守る。**

| diff の内容 | 要求する version |
|---|---|
| `appVersion` だけ (経路 A: release-please) | 不変 または patch |
| **image tag 行だけ**と構文的に確認できた (経路 B: renovate) | patch |
| templates / values / `.helmignore` の変更 (経路 C: 人手) | minor または major |
| **上記に分類されない梱包物の変更 (catch-all)** | minor または major |
| 梱包されないファイルだけ | 不問 |

**catch-all を必ず持たせる。** `Chart.yaml` の `dependencies`、`Chart.lock`、`crds/`、
`files/`、subchart、追加の values ファイルなど未分類の梱包物変更は minor/major 要求に倒す。
**patch を許可するのは「image tag だけ」と構文的に確認できた場合に限る。**

梱包されるファイルの集合は base 側と head 側の両方を `helm package` して和集合で求めるので、
`.helmignore` を正しく反映し、削除されたファイルも拾える。`.helmignore` 自体は tgz に
入らないが、その変更は梱包内容そのものを変えるので構造変更として扱う。

```yaml
chart-guard:
  uses: Soli0222/shared-workflows/.github/workflows/chart-guard.yaml@v1.0.1
```

### `weekly-merge.yaml`

各リポジトリに分散していた週次 release-please PR 自動マージの共通化版。
起動経路 (schedule / workflow_dispatch) と RENOVATE 系 secrets は呼び出し元側。

ブランチ判定は `release-please--branches--<既定ブランチ>` または
その `--components--<component>` 付きのものに限定する (manifest モードで
release-type: node 等は component がブランチ名に入るため両方必要)。
あわせて `autorelease: pending` ラベル、draft 以外、`RELEASE_APP_SLUG`
(App token の slug) と一致する bot 作者であることを要求する。
check が1つ以上あり、すべて SUCCESS / NEUTRAL / SKIPPED で完了していること・
`mergeStateStatus == CLEAN` を確認してから、検証した head SHA を pin して squash merge する。
特定の check を必須にしたいときは `required-checks` に名前を渡す (改行かカンマ区切り。
例: `required-checks: CI required`)。列挙した check は存在し、SUCCESS である必要がある。
マージしなかったときは理由を warning と Step Summary に残す (run は成功のまま)。

呼び出し元の `weekly-release.yaml` はこれだけあればよい:

```yaml
name: Weekly Release PR Merge
on:
  schedule:
    - cron: '0 22 * * 6' # Sunday 07:00 JST
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: weekly-release-${{ github.repository }}
  cancel-in-progress: false

jobs:
  merge:
    permissions:
      contents: write
      pull-requests: write
      checks: read
      statuses: read
    uses: Soli0222/shared-workflows/.github/workflows/weekly-merge.yaml@v1.2.0
    secrets: inherit
```

### `release-pipeline.yaml`

各リポジトリに分散していたリリース DAG の共通化版。
`push: <既定ブランチ>` から起動し、release-please → image publish → chart publish を
1 run でつなぐ。RENOVATE 系 secrets は呼び出し元から `secrets: inherit` で渡す。

- `version-check-command` … publish 前の版数検証 (例: sui/hokan の `make version-check`)。
- `advance-latest` … 安定版リリースで `ghcr.io/<repo>:latest` を進める。
- `platforms` / `image-name` / `context` / `dockerfile` … `publish-image` に転送。
- `chart-name` / `chart-dir` / `oci-repository` / `api-versions` / `extra-images` … `publish-chart` に転送。
- `release-tools-prefix` + `node-version` … release-please 前に `npm ci` する
  リポジトリ (psr-node 系) 用。

呼び出し元の `release.yaml` はこれだけあればよい:

```yaml
name: Release Pipeline
on:
  push:
    branches: [main] # summaly は master
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: release-pipeline-${{ github.repository }}
  queue: max

jobs:
  release:
    permissions:
      contents: write
      pull-requests: write
      packages: write
    uses: Soli0222/shared-workflows/.github/workflows/release-pipeline.yaml@v1.3.0
    secrets: inherit
    with:
      api-versions: monitoring.coreos.com/v1 # chart で CRD を使うリポジトリだけ
```

### `release-image.yaml`

`publish-image` の前後にリリース固有の補助 (任意の `version-check-command` と
`advance-latest`) を付けた薄いラッパー。`release-pipeline` / `release-retry` から
呼ばれるほか、repo 独自の起動経路から直接呼んでもよい。

### `release-retry.yaml`

既存リリースに対して image / chart publish だけをやり直す。
tag → sha の解決は workflow 内の vendored script が行う。

呼び出し元の `release-retry.yaml`:

```yaml
name: Retry Release Publish
on:
  workflow_dispatch:
    inputs:
      version:
        description: Existing release version without v (including RC versions)
        required: true
        type: string

permissions:
  contents: read

jobs:
  retry:
    permissions:
      contents: write
      pull-requests: write
      packages: write
    uses: Soli0222/shared-workflows/.github/workflows/release-retry.yaml@v1.3.0
    secrets: inherit
    with:
      version: ${{ inputs.version }}
      version-check-command: 'make version-check VERSION="$VERSION"'
      advance-latest: true
```

### `chart-release.yaml`

`push: charts/**` / 定期実行 / dispatch の起動経路と、release-please の
appVersion-only push を捌く triage をまとめたもの。triage は vendored bash で、
per-repo の Python 実装と同じ判定 (Chart.yaml 1 ファイル・appVersion 行のみの
変更は DAG 側に任せる)。

呼び出し元の `chart-release.yaml`:

```yaml
name: Publish Helm Chart
on:
  push:
    branches: [main]
    paths: ['charts/**']
  schedule:
    - cron: '17 19 * * *' # Daily 04:17 JST
  workflow_dispatch:

permissions:
  contents: read

jobs:
  publish:
    permissions:
      contents: write
      packages: write
    uses: Soli0222/shared-workflows/.github/workflows/chart-release.yaml@v1.3.0
    secrets: inherit
    with:
      api-versions: monitoring.coreos.com/v1
```

### `goreleaser.yaml`

Go バイナリを GitHub Release へ公開する。`tag` input に
`inputs.tag || github.ref_name` を渡す。`go-version` を指定しない場合は
`go-version-file` (既定 `go.mod`) を参照する。`app-token: true` で
GitHub App token を `APP_TOKEN` env に載せる (brews の tap push 用、
`extra-token-repositories` で push 先リポジトリを追加スコープする)。
secrets は `APP_ID`/`PRIVATE_KEY` を優先し、無ければ
`RENOVATE_CLIENT_ID`/`RENOVATE_PRIVATE_KEY` にフォールバックする。

呼び出し元の `release.yaml`:

```yaml
name: goreleaser
on:
  workflow_dispatch:
    inputs:
      tag:
        description: 'Existing published release tag to publish binaries for'
        required: true
        type: string
  push:
    tags: ['v*']

permissions:
  contents: write

jobs:
  goreleaser:
    uses: Soli0222/shared-workflows/.github/workflows/goreleaser.yaml@v1.3.0
    secrets: inherit
    with:
      tag: ${{ inputs.tag || github.ref_name }}
```

## テスト

`chart-guard` の分類は 9 リポジトリの required check になるので、
`tests/chart-guard/` に実際の git リポジトリを作って全経路を回すケーステストがある。
テストは workflow YAML から実行スクリプトを抜き出して動かすので、
**CI が実際に流すコードそのもの**を検証する。

`weekly-merge` の候補選択と CI gate も `tests/weekly-merge/cases.py` で
workflow から抜き出したスクリプトを stub した `gh` で実走させて検証する。
`publish-chart` の調停も同様に `tests/publish-chart/cases.py` で、OCI だけを stub して
Git と Helm は実物で動かす (レジストリのエラーや push の失敗で止まることも確認する)。

`tests/job-conditions/cases.py` は、`needs.<job>.result == 'skipped'` を見る job の `if` に
ステータス関数 (`!cancelled()` など) が付いているかを静的に検査する。付いていないと
暗黙の `success() &&` によって needs が skipped のときに job が黙って skip される。

```console
$ tests/chart-guard/extract.sh
$ tests/chart-guard/setup.sh
$ tests/chart-guard/cases.sh
$ python3 tests/weekly-merge/cases.py
$ python3 tests/publish-chart/cases.py
$ python3 tests/job-conditions/cases.py
```
