"""Exercise the vendored chart-release triage script against a real git repo.

The run: block is extracted from the workflow itself and executed for real in a
fixture repository; only git history and EVENT/BEFORE/AFTER env are fabricated.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]

CHART = "name: app\nversion: 0.1.0\nappVersion: \"1.0.0\"\n"


def script():
    workflow = (ROOT / ".github/workflows/chart-release.yaml").read_text()
    body = workflow.split("        run: |\n", 1)[1]
    end = next(i for i, l in enumerate(body.splitlines()) if l and not l.startswith(" " * 8))
    return textwrap.dedent("\n".join(body.splitlines()[:end]))


class ChartTriageTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name) / "repo"
        self.repo.mkdir()
        self.out = Path(self._td.name) / "out.txt"
        for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            self.git(*args)
        chart = self.repo / "charts" / "app" / "Chart.yaml"
        chart.parent.mkdir(parents=True)
        chart.write_text(CHART)
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.base = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self._td.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, text=True, capture_output=True, check=True
        ).stdout

    def commit(self, path, content):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-qm", "change")
        return self.git("rev-parse", "HEAD").strip()

    def decide(self, event="push", before=None, after=None):
        env = dict(os.environ)
        env.update({
            "EVENT": event,
            "BEFORE": before if before is not None else self.base,
            "AFTER": after if after is not None else self.git("rev-parse", "HEAD").strip(),
            "GITHUB_OUTPUT": str(self.out),
        })
        proc = subprocess.run(
            ["bash", "-c", script()], cwd=self.repo, text=True, capture_output=True, env=env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.out.read_text().strip(), proc.stdout

    def test_non_push_always_runs(self):
        out, _ = self.decide(event="schedule")
        self.assertIn("run=true", out)

    def test_unresolvable_before_runs(self):
        out, _ = self.decide(before="0" * 40)
        self.assertIn("run=true", out)

    def test_appversion_only_change_is_skipped(self):
        after = self.commit(
            "charts/app/Chart.yaml", CHART.replace('"1.0.0"', '"1.1.0"'))
        out, _ = self.decide(after=after)
        self.assertIn("run=false", out)

    def test_appversion_plus_other_key_runs(self):
        after = self.commit(
            "charts/app/Chart.yaml",
            CHART.replace('"1.0.0"', '"1.1.0"').replace("0.1.0", "0.1.1"))
        out, _ = self.decide(after=after)
        self.assertIn("run=true", out)

    def test_chart_yaml_plus_other_file_runs(self):
        self.commit("charts/app/Chart.yaml", CHART.replace('"1.0.0"', '"1.1.0"'))
        after = self.commit("charts/app/values.yaml", "image: app:1.1.0\n")
        # before = base covers both commits -> two changed files
        out, _ = self.decide(after=after)
        self.assertIn("run=true", out)

    def test_values_only_change_runs(self):
        after = self.commit("charts/app/values.yaml", "image: app:1.1.0\n")
        out, _ = self.decide(after=after)
        self.assertIn("run=true", out)

    def test_change_outside_charts_runs(self):
        after = self.commit("README.md", "x\n")
        out, _ = self.decide(after=after)
        self.assertIn("run=true", out)


if __name__ == "__main__":
    unittest.main()
