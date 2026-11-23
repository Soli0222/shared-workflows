"""Exercise the actual reconciliation script against main/master Git remotes.

Only OCI transport is stubbed; Git, Helm packaging/rendering, version bumping and
commit-back run for real, without registry credentials or network access.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]

# The stub answers like ghcr.io:
# - tags/list: CURL_TAGS_CODE (default 200) with the tags in CURL_TAGS (comma separated)
# - manifests/<version>: CURL_MANIFEST_CODE (default 200)
# The status code is printed only when the script asks for it with -w.
CURL_STUB = r'''#!/bin/bash
out="" write=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -w) write="$2"; shift 2;;
    -H|-u) shift 2;;
    -*) shift;;
    *) url="$1"; shift;;
  esac
done
case "$url" in
  *tags/list*)
    code="${CURL_TAGS_CODE:-200}"
    body=$(jq -cn --arg tags "${CURL_TAGS-0.1.0}" '{tags: ($tags | split(",") | map(select(length > 0)))}')
    ;;
  *manifests*) code="${CURL_MANIFEST_CODE:-200}"; body='{}';;
  *) code=200; body='{"token":"test"}';;
esac
if [[ -n "$out" ]]; then printf '%s' "$body" > "$out"; else printf '%s' "$body"; fi
[[ -n "$write" ]] && printf '%s' "$code"
exit 0
'''

HELM_STUB = '''#!/bin/bash
case "$1" in
  pull) while [[ "$1" != --untardir ]]; do shift; done; mkdir -p "$2"; tar xzf "$FIXTURE_CHART" -C "$2";;
  push) [[ -n "$HELM_PUSH_FAIL" ]] && { echo "push failed" >&2; exit 1; }; echo "push $*" >> "$HELM_PUSH_LOG";;
  show) exit 0;;
  template) [[ -n "$HELM_TEMPLATE_FAIL" ]] && { echo "template failed" >&2; exit 1; }; exec "$REAL_HELM" "$@";;
  *) exec "$REAL_HELM" "$@";;
esac
'''


def script():
    workflow = (ROOT / '.github/workflows/publish-chart.yaml').read_text()
    return textwrap.dedent(workflow.split('      - name: Reconcile\n', 1)[1].split('        run: |\n', 1)[1])


class ReconcileTest(unittest.TestCase):
    def reconcile(self, branch='main', **env_overrides):
        """Publish sample-0.1.0 (appVersion 1.0.0) as a fixture, change appVersion to 1.0.1 in Git and reconcile."""
        root = self.root

        def run(*args, cwd=root, **kwargs):
            try:
                return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.STDOUT, **kwargs)
            except subprocess.CalledProcessError as error:
                raise AssertionError(error.output) from error

        run('git', 'init', '--bare', '--initial-branch=' + branch, 'remote.git')
        run('git', 'clone', str(root / 'remote.git'), 'work')
        work = root / 'work'
        run('git', 'config', 'commit.gpgsign', 'false', cwd=work)
        run('git', 'config', 'user.name', 'test', cwd=work)
        run('git', 'config', 'user.email', 'test@example.com', cwd=work)
        chart = work / 'charts/sample'
        (chart / 'templates').mkdir(parents=True)
        (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: sample\nversion: 0.1.0\nappVersion: "1.0.0"\n')
        (chart / 'templates/pod.yaml').write_text('apiVersion: v1\nkind: Pod\nmetadata:\n  name: sample\nspec:\n  containers:\n    - name: sample\n      image: "example/sample:{{ .Chart.AppVersion }}"\n')
        run('helm', 'package', str(chart), '--destination', str(root))
        (chart / 'Chart.yaml').write_text((chart / 'Chart.yaml').read_text().replace('1.0.0', '1.0.1'))
        run('git', 'add', '.', cwd=work)
        run('git', 'commit', '-m', 'fix: update application', cwd=work)
        run('git', 'push', 'origin', branch, cwd=work)

        bin_dir = root / 'bin'
        bin_dir.mkdir()
        for name, body in {'curl': CURL_STUB, 'docker': '#!/bin/bash\nexit 0\n', 'helm': HELM_STUB}.items():
            p = bin_dir / name
            p.write_text(body)
            p.chmod(0o755)

        # Keep the workflow's fixed temporary paths isolated in this fixture.
        fixture_script = script().replace('/tmp/chart-compare', str(root / 'compare')).replace('/tmp/dist', str(root / 'dist'))
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                   DEFAULT_BRANCH=branch, CHART_NAME_IN='', CHART_DIR_IN='', OCI_REPO_IN='',
                   API_VERSIONS='', EXTRA_IMAGES='', REPOSITORY='owner/sample', OWNER='owner',
                   ACTOR='test', GITHUB_TOKEN='test', GITHUB_OUTPUT=str(root / 'outputs'),
                   REAL_HELM=shutil.which('helm'), FIXTURE_CHART=str(root / 'sample-0.1.0.tgz'),
                   HELM_PUSH_LOG=str(root / 'pushed.log'))
        env.update(env_overrides)
        proc = subprocess.run(['bash', '-c', fixture_script], cwd=work, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        outputs = (root / 'outputs').read_text() if (root / 'outputs').exists() else ''
        pushed = (root / 'pushed.log').read_text() if (root / 'pushed.log').exists() else ''
        remote_chart = run('git', '--git-dir=' + str(root / 'remote.git'), 'show', f'{branch}:charts/sample/Chart.yaml')
        return proc, outputs, pushed, remote_chart

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_reconcile_and_commit_back_to_caller_branch(self):
        for branch in ['main', 'master']:
            with self.subTest(branch=branch):
                self.tearDown()
                self.setUp()
                proc, outputs, pushed, remote_chart = self.reconcile(branch)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertIn('result=published', outputs, proc.stdout)
                self.assertIn('sample-0.1.1.tgz', pushed)
                self.assertIn('version: 0.1.1', remote_chart)
                self.assertIn('1.0.1', remote_chart)
                branches = subprocess.check_output(
                    ['git', '--git-dir=' + str(self.root / 'remote.git'), 'branch', '--format=%(refname:short)'], text=True)
                self.assertEqual(branches.strip(), branch)

    def test_missing_version_is_published_as_is(self):
        proc, outputs, pushed, remote_chart = self.reconcile(CURL_MANIFEST_CODE='404')
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn('result=published', outputs, proc.stdout)
        self.assertIn('sample-0.1.0.tgz', pushed)
        self.assertIn('version: 0.1.0', remote_chart)

    def test_first_publish_of_a_new_package(self):
        # ghcr.io answers 403 (not 404) for a package that does not exist yet.
        proc, outputs, pushed, _ = self.reconcile(CURL_TAGS_CODE='403', CURL_TAGS='', CURL_MANIFEST_CODE='403')
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn('result=published', outputs, proc.stdout)
        self.assertIn('sample-0.1.0.tgz', pushed)

    def test_manifest_lookup_error_does_not_publish(self):
        # A registry error must not be taken as "not published", or an existing version is overwritten.
        for code in ['500', '403']:
            with self.subTest(code=code):
                self.tearDown()
                self.setUp()
                proc, outputs, pushed, remote_chart = self.reconcile(CURL_MANIFEST_CODE=code)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertNotIn('result=published', outputs)
                self.assertEqual(pushed, '')
                self.assertIn('version: 0.1.0', remote_chart)

    def test_tags_list_error_does_not_publish(self):
        proc, outputs, pushed, _ = self.reconcile(CURL_TAGS_CODE='502')
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn('result=published', outputs)
        self.assertEqual(pushed, '')

    def test_push_failure_fails_the_run(self):
        proc, outputs, _, _ = self.reconcile(HELM_PUSH_FAIL='1')
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn('result=published', outputs)

    def test_render_failure_does_not_skip_the_image_check(self):
        proc, outputs, pushed, remote_chart = self.reconcile(HELM_TEMPLATE_FAIL='1')
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertNotIn('result=published', outputs)
        self.assertEqual(pushed, '')
        self.assertIn('version: 0.1.0', remote_chart)


if __name__ == '__main__':
    unittest.main()
