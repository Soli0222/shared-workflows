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


class DefaultBranchTest(unittest.TestCase):
    def test_reconcile_and_commit_back_to_caller_branch(self):
        workflow = (ROOT / '.github/workflows/publish-chart.yaml').read_text()
        script = textwrap.dedent(workflow.split('      - name: Reconcile\n', 1)[1].split('        run: |\n', 1)[1])
        for branch in ['main', 'master']:
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                def run(*args, cwd=root, **kwargs):
                    try:
                        return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.STDOUT, **kwargs)
                    except subprocess.CalledProcessError as error:
                        raise AssertionError(error.output) from error
                run('git', 'init', '--bare', '--initial-branch='+branch, 'remote.git')
                run('git', 'clone', str(root/'remote.git'), 'work')
                work = root/'work'
                run('git', 'config', 'commit.gpgsign', 'false', cwd=work)
                run('git', 'config', 'user.name', 'test', cwd=work)
                run('git', 'config', 'user.email', 'test@example.com', cwd=work)
                chart = work/'charts/sample'
                (chart/'templates').mkdir(parents=True)
                (chart/'Chart.yaml').write_text('apiVersion: v2\nname: sample\nversion: 0.1.0\nappVersion: "1.0.0"\n')
                (chart/'templates/pod.yaml').write_text('apiVersion: v1\nkind: Pod\nmetadata:\n  name: sample\nspec:\n  containers:\n    - name: sample\n      image: "example/sample:{{ .Chart.AppVersion }}"\n')
                run('helm', 'package', str(chart), '--destination', str(root))
                (chart/'Chart.yaml').write_text((chart/'Chart.yaml').read_text().replace('1.0.0', '1.0.1'))
                run('git', 'add', '.', cwd=work)
                run('git', 'commit', '-m', 'fix: update application', cwd=work)
                run('git', 'push', 'origin', branch, cwd=work)
                bin_dir = root/'bin'
                bin_dir.mkdir()
                wrappers = {
                    'curl': '#!/bin/bash\ncase "$*" in *tags/list*) echo \'{"tags":["0.1.0"]}\';; *manifests*) exit 0;; *) echo \'{"token":"test"}\';; esac\n',
                    'docker': '#!/bin/bash\nexit 0\n',
                    'helm': '''#!/bin/bash
case "$1" in
  pull) while [[ "$1" != --untardir ]]; do shift; done; mkdir -p "$2"; tar xzf "$FIXTURE_CHART" -C "$2";;
  push|show) exit 0;;
  *) exec "$REAL_HELM" "$@";;
esac
''',
                }
                for name, body in wrappers.items():
                    p = bin_dir/name
                    p.write_text(body)
                    p.chmod(0o755)
                # Keep the workflow's fixed temporary paths isolated in this fixture.
                fixture_script = script.replace('/tmp/chart-compare', str(root/'compare')).replace('/tmp/dist', str(root/'dist'))
                env = dict(os.environ, PATH=str(bin_dir)+os.pathsep+os.environ['PATH'],
                           DEFAULT_BRANCH=branch, CHART_NAME_IN='', CHART_DIR_IN='', OCI_REPO_IN='',
                           API_VERSIONS='', EXTRA_IMAGES='', REPOSITORY='owner/sample', OWNER='owner',
                           ACTOR='test', GITHUB_TOKEN='test', GITHUB_OUTPUT=str(root/'outputs'),
                           REAL_HELM=shutil.which('helm'), FIXTURE_CHART=str(root/'sample-0.1.0.tgz'))
                output = run('bash', '-c', fixture_script, cwd=work, env=env)
                self.assertIn('result=published', (root/'outputs').read_text(), output)
                remote_chart = run('git', '--git-dir='+str(root/'remote.git'), 'show', f'{branch}:charts/sample/Chart.yaml')
                self.assertIn('version: 0.1.1', remote_chart)
                self.assertIn('1.0.1', remote_chart)
                self.assertEqual(run('git', '--git-dir='+str(root/'remote.git'), 'branch', '--format=%(refname:short)').strip(), branch)


if __name__ == '__main__':
    unittest.main()
