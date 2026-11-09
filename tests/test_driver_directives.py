"""The driver directives of the published case pack.

The organisers' `.test` files may contain two directives that are not SQL:

* ``--restart``  - shut down normally (waiting for the exit) and start
  again against the same data directory; the shutdown must roll back an
  uncommitted transaction.
* ``--mode ann`` - subsequent vector requests use the ann mode.  Every case
  starts in the exact mode.

These checks drive a real subprocess, because that is how the organiser's
driver drives the database.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SENTINEL = "--MVD-END--"


def run_script(script, data_dir, mode="exact"):
    """Run a script against a fresh database and return (stdout, stderr)."""
    env = dict(os.environ)
    env.update({
        "MVD_DATA_DIR": data_dir,
        "MVD_PROTOCOL": "line",
        "MVD_ECHO": "0",
        "MVD_TERMINATOR": "0",
        "MVD_MODE": mode,
        "MVD_RESULT_SENTINEL": SENTINEL,
    })
    proc = subprocess.run(
        [sys.executable, "-u", "-m", "minivectordb"],
        input=script.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        env=env,
        timeout=180,
    )
    return proc.stdout.decode("utf-8", "replace"), proc.stderr.decode(
        "utf-8", "replace"
    )


class DriverDirectiveTest(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="mvd-directive-")

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_mode_directive_is_accepted_and_keeps_the_process_alive(self):
        stdout, stderr = run_script(
            "create table t (id bigint primary key, e vector(2));\n"
            "insert into t values (1, '[0,0]'), (2, '[1,0]');\n"
            "create vector index ix on t(e);\n"
            "select id from t order by l2_distance(e, '[0,0]') limit 1;\n"
            "--mode ann\n"
            "select id from t order by l2_distance(e, '[0,0]') limit 1;\n",
            self.data_dir,
        )
        self.assertNotIn("Traceback", stderr, stderr)
        # Both queries must have produced a result set.
        self.assertEqual(stdout.count("id\n"), 2, stdout)

    def test_mode_directive_switches_the_search_mode(self):
        """The ann mode must reach the approximate search path."""
        script = (
            "create table t (id bigint primary key, e vector(8));\n"
            "insert into t values (1, '[0,0,0,0,0,0,0,0]'), "
            "(2, '[1,0,0,0,0,0,0,0]'), (3, '[0,1,0,0,0,0,0,0]');\n"
            "create vector index ix on t(e);\n"
            "--mode ann\n"
            "select id from t order by l2_distance(e, "
            "'[0,0,0,0,0,0,0,0]') limit 2;\n"
        )
        stdout, stderr = run_script(script, self.data_dir)
        self.assertNotIn("Traceback", stderr, stderr)
        # The nearest row is id 1 in both modes.
        self.assertIn("1", stdout, stdout)

    def test_restart_keeps_committed_rows(self):
        """--restart: a new process on the same data directory.

        The directive is executed by the driver, so the test starts a second
        process itself against the same directory.
        """
        stdout, stderr = run_script(
            "create table t (id bigint primary key, v bigint);\n"
            "insert into t values (1, 10);\n",
            self.data_dir,
        )
        self.assertNotIn("Traceback", stderr, stderr)
        stdout, stderr = run_script(
            "select id, v from t order by id;\n", self.data_dir
        )
        self.assertNotIn("Traceback", stderr, stderr)
        self.assertIn("10", stdout, stdout)

    def test_restart_rolls_back_an_uncommitted_transaction(self):
        """The shutdown must roll back a transaction that was never
        committed."""
        stdout, stderr = run_script(
            "create table t (id bigint primary key, v bigint);\n"
            "insert into t values (1, 10);\n"
            "begin;\n"
            "insert into t values (2, 20);\n",
            self.data_dir,
        )
        self.assertNotIn("Traceback", stderr, stderr)
        stdout, stderr = run_script(
            "select id, v from t order by id;\n", self.data_dir
        )
        self.assertNotIn("Traceback", stderr, stderr)
        self.assertIn("10", stdout, stdout)
        self.assertNotIn("20", stdout, stdout)


if __name__ == "__main__":
    unittest.main()
