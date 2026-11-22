"""Exercise the vendored weekly-merge script with a stubbed `gh` CLI.

The run: block is extracted from the workflow itself (same as publish-chart
tests) and executed for real; only `gh` is replaced by a PATH shim so the
candidate selection and CI gate logic run unmodified.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]

APP = "pke-renovate"
BOT_PR = {
    "number": 772,
    "headRefName": "release-please--branches--main--components--app",
    "isDraft": False,
    "author": {"login": "pke-renovate[bot]", "is_bot": True},
}
READY_VIEW = {
    "headRefOid": "abc123",
    "mergeStateStatus": "CLEAN",
    "statusCheckRollup": [
        {"name": "CI required", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "optional", "status": "COMPLETED", "conclusion": "SKIPPED"},
    ],
}

GH_STUB = """#!/bin/bash
if [ "$1" = "pr" ] && [ "$2" = "list" ]; then printf '%s' "$GH_STUB_LIST"; exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then printf '%s' "$GH_STUB_VIEW"; exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then echo "merge $*" >> "$GH_MERGED_LOG"; exit 0; fi
echo "unexpected gh call: $*" >&2; exit 1
"""


def script():
    workflow = (ROOT / ".github/workflows/weekly-merge.yaml").read_text()
    body = workflow.split("        run: |\n", 1)[1]
    return textwrap.dedent(body)


class WeeklyMergeTest(unittest.TestCase):
    def run_merge(self, prs, view=READY_VIEW):
        tmp = self.tmp
        gh = Path(tmp) / "bin" / "gh"
        gh.parent.mkdir(parents=True, exist_ok=True)
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        merged_log = Path(tmp) / "merged.log"
        env = dict(os.environ)
        env.update({
            "PATH": f"{gh.parent}:{env['PATH']}",
            "GITHUB_REPOSITORY": "owner/app",
            "RELEASE_APP_SLUG": APP,
            "BASE_BRANCH": "main",
            "GH_STUB_LIST": json.dumps(prs),
            "GH_STUB_VIEW": json.dumps(view),
            "GH_MERGED_LOG": str(merged_log),
            "GH_TOKEN": "x",
        })
        proc = subprocess.run(
            ["bash", "-c", script()], text=True, capture_output=True, env=env)
        return proc, merged_log.read_text() if merged_log.exists() else ""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def merged(self, log):
        return "--squash" in log

    def test_components_suffix_branch_merges(self):
        proc, log = self.run_merge([BOT_PR])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.merged(log), proc.stdout)
        self.assertIn("--match-head-commit abc123", log)

    def test_plain_branch_merges(self):
        pr = dict(BOT_PR, headRefName="release-please--branches--main")
        proc, log = self.run_merge([pr])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.merged(log), proc.stdout)

    def test_other_base_branch_ignored(self):
        pr = dict(BOT_PR, headRefName="release-please--branches--dev--components--app")
        proc, log = self.run_merge([pr])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.merged(log))
        self.assertIn("No pending release-please PR.", proc.stdout)

    def test_non_bot_author_ignored(self):
        pr = dict(BOT_PR, author={"login": "someone", "is_bot": False})
        proc, log = self.run_merge([pr])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.merged(log))
        self.assertIn("No pending release-please PR.", proc.stdout)

    def test_wrong_app_slug_ignored(self):
        pr = dict(BOT_PR, author={"login": "other-app[bot]", "is_bot": True})
        proc, log = self.run_merge([pr])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.merged(log))

    def test_draft_ignored(self):
        pr = dict(BOT_PR, isDraft=True)
        proc, log = self.run_merge([pr])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.merged(log))

    def test_multiple_candidates_refused(self):
        second = dict(BOT_PR, number=773)
        proc, _ = self.run_merge([BOT_PR, second])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Multiple release PRs found", proc.stderr)

    def test_missing_required_check_leaves_open(self):
        view = dict(READY_VIEW, statusCheckRollup=[
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        proc, log = self.run_merge([BOT_PR], view)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.merged(log))
        self.assertIn("leaving it open", proc.stdout)

    def test_failing_check_leaves_open(self):
        view = dict(READY_VIEW, statusCheckRollup=READY_VIEW["statusCheckRollup"] + [
            {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
        ])
        proc, log = self.run_merge([BOT_PR], view)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.merged(log))

    def test_dirty_merge_state_leaves_open(self):
        view = dict(READY_VIEW, mergeStateStatus="BEHIND")
        proc, log = self.run_merge([BOT_PR], view)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.merged(log))


if __name__ == "__main__":
    unittest.main()
