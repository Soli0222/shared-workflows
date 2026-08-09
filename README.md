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

呼び出しは `@<sha>` で pin する。

## workflow

| workflow | 役割 |
|---|---|
| `publish-image.yaml` | multi-arch image を build して `ghcr.io` へ publish する |
| `publish-chart.yaml` | main 上の chart を `oci://ghcr.io/<owner>/charts` へ収束させる (調停処理) |
| `chart-guard.yaml` | PR の chart 変更と version の上げ方が噛み合っているかを検査する |

### `publish-image.yaml`

```yaml
publish-image:
  needs: release-please
  if: needs.release-please.outputs.release-created == 'true'
  permissions:
    contents: read
    packages: write
  uses: Soli0222/shared-workflows/.github/workflows/publish-image.yaml@<sha>
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
image publish 成功を契機として、**main 上の最新 chart を OCI へ収束させる**処理である。
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
  uses: Soli0222/shared-workflows/.github/workflows/publish-chart.yaml@<sha>
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
  uses: Soli0222/shared-workflows/.github/workflows/chart-guard.yaml@<sha>
```

## テスト

`chart-guard` の分類は 9 リポジトリの required check になるので、
`tests/chart-guard/` に実際の git リポジトリを作って全経路を回すケーステストがある。
テストは workflow YAML から実行スクリプトを抜き出して動かすので、
**CI が実際に流すコードそのもの**を検証する。

```console
$ tests/chart-guard/extract.sh
$ tests/chart-guard/setup.sh
$ tests/chart-guard/cases.sh
```
