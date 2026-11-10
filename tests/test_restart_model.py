"""The public case pack under the alternative --restart driver model.

The published pack defines --restart as "shut down normally and wait for the
exit, then start again against the same data directory".  That can be read
two ways, and the database has to work under both:

  A) the driver stops the process and starts a new one;
  B) the driver sends the literal --restart and expects the database to shut
     itself down.

Under model B the database rolls back any open transaction, flushes and
exits 0, so the driver can start the next process on the same data
directory.  Model A is covered by tests/run_official_cases.py.

The check itself lives in tests/restart_model_check.py; it is a script
rather than a unittest module because it drives real processes.
"""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class RestartModelTest(unittest.TestCase):
    def test_every_case_passes_under_model_b(self):
        script = os.path.join(HERE, "restart_model_check.py")
        proc = subprocess.run(
            [sys.executable, script],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
        output = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(
            proc.returncode, 0,
            "the --restart model B check failed:\n" + output,
        )
        self.assertIn("model-B failures: 0", output, output)


if __name__ == "__main__":
    unittest.main()
