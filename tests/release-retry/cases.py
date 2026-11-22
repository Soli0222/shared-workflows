"""Exercise the vendored release-retry resolve script with a stubbed `gh` CLI.

The run: block is extracted from the workflow itself and executed for real;
only `gh` is replaced by a PATH shim and the git repo is a fixture.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]

GH_STUB = """#!/bin/bash
if [ "$1" = "release" ] && [ "$2" = "view" ]; then printf '%s' "$GH_STUB_VIEW"; exit $GH_STUB_RC; fi
echo "unexpected gh call: $*" >&2; exit 1
"""


def script():
    workflow = (ROOT / ".github/workflows/release-retry.yaml").read_text()
    body = workflow.split("        run: |\n", 1)[1]
    end = next(i for i, l in enumerate(body.splitlines()) if l and not l.startswith(" " * 8))
    return textwrap.dedent("\n".join(body.splitlines()[:end]))


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        tmp = self._td.name
        self.repo = Path(tmp) / "repo"
        self.repo.mkdir()
        self.out = Path(tmp) / "out.txt"
        for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            self.git(*args)
        (self.repo / "f").write_text("x")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git("tag", "1.2.3")

        gh = Path(tmp) / "bin" / "gh"
        gh.parent.mkdir()
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.bindir = gh.parent

    def tearDown(self):
        self._td.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout

    def resolve(self, version, view=None, gh_rc=0):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bindir}:{env['PATH']}",
            "VERSION": version,
            "GH_TOKEN": "x",
            "GITHUB_REPOSITORY": "owner/app",
            "GITHUB_OUTPUT": str(self.out),
            "GH_STUB_VIEW": json.dumps(view) if view else "",
            "GH_STUB_RC": str(gh_rc),
        })
        self.out.write_text("")
        return subprocess.run(
            ["bash", "-c", script()], cwd=self.repo, text=True, capture_output=True, env=env
        )

    def test_resolves_published_release(self):
        proc = self.resolve("1.2.3", view={"tagName": "1.2.3", "isDraft": False})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"sha={self.sha}", self.out.read_text())

    def test_rc_version_resolves(self):
        self.git("commit", "-q", "--allow-empty", "-m", "rc")
        self.git("tag", "2.0.0-rc.1")
        sha = self.git("rev-parse", "HEAD").strip()
        proc = self.resolve("2.0.0-rc.1", view={"tagName": "2.0.0-rc.1", "isDraft": False})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"sha={sha}", self.out.read_text())

    def test_rejects_v_prefix(self):
        proc = self.resolve("v1.2.3", view={"tagName": "v1.2.3", "isDraft": False})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("without a v prefix", proc.stderr)

    def test_rejects_non_semver(self):
        proc = self.resolve("latest", view={})
        self.assertNotEqual(proc.returncode, 0)

    def test_draft_release_rejected(self):
        proc = self.resolve("1.2.3", view={"tagName": "1.2.3", "isDraft": True})
        self.assertNotEqual(proc.returncode, 0)

    def test_tag_name_mismatch_rejected(self):
        proc = self.resolve("1.2.3", view={"tagName": "v1.2.3", "isDraft": False})
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_release_fails(self):
        proc = self.resolve("1.2.3", view="", gh_rc=1)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
