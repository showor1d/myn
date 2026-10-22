"""Host-owned unittest entry point; collect actual execution instead of parsing claims."""

import json
import sys
import unittest
from pathlib import Path


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed = []

    def startTest(self, test):
        self.executed.append(str(test))
        super().startTest(test)


def main(base, destination):
    test_dir = Path(base) / "tests"
    sys.path.insert(0, str(test_dir))
    suite = unittest.defaultTestLoader.discover(str(test_dir))
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    failures = [str(test) for test, _ in result.failures + result.errors]
    failures.extend(str(test) for test in result.unexpectedSuccesses)
    report = {"complete": True, "ran": result.testsRun, "tests": result.executed,
              "skipped": [str(test) for test, _ in result.skipped], "failures": failures}
    Path(destination).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
