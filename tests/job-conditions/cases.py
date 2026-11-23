"""Check job-level `if` conditions that expect a needed job to be skipped.

A job `if` without a status check function is implicitly prefixed with
`success() &&`, which is false when any needed job was skipped. A condition such
as `needs.x.result == 'skipped'` is then never true, and the job is skipped
silently (the whole run still succeeds). Such conditions must call a status
check function explicitly (e.g. `!cancelled()`).
"""
import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
STATUS_FUNCTIONS = re.compile(r'\b(always|cancelled|failure|success)\(\)')
EXPECTS_SKIPPED = re.compile(r"needs\.[\w-]+\.result\s*[!=]=\s*'skipped'")


def jobs(path):
    data = json.loads(subprocess.check_output(['yq', '-o=json', '.', str(path)], text=True))
    return (data.get('jobs') or {}).items()


def offending(condition):
    return bool(EXPECTS_SKIPPED.search(condition)) and not STATUS_FUNCTIONS.search(condition)


class JobConditionsTest(unittest.TestCase):
    def test_detector(self):
        self.assertTrue(offending("needs.a.result == 'success' || needs.a.result == 'skipped'"))
        self.assertFalse(offending("${{ !cancelled() && (needs.a.result == 'success' || needs.a.result == 'skipped') }}"))
        self.assertFalse(offending("needs.a.result == 'success'"))

    def test_workflows(self):
        for path in sorted((ROOT / '.github/workflows').glob('*.yaml')):
            for name, job in jobs(path):
                with self.subTest(workflow=path.name, job=name):
                    condition = str(job.get('if', ''))
                    self.assertFalse(
                        offending(condition),
                        f"{path.name} jobs.{name}.if needs a status check function such as !cancelled(): {condition}",
                    )


if __name__ == '__main__':
    unittest.main()
