"""The JSON Lines protocol of the judge.

The protocol is specified by the organisers in ``STDIO_PROTOCOL.md`` and
implemented by their ``protocol_check`` driver.  These checks drive the
database the way the driver does: one JSON request per line, one JSON
response per line, with the startup handshake first.
"""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


class JSONLProtocolTest(unittest.TestCase):
    """Drive a real server process over the JSONL protocol."""

    def _run(self, requests, expect_exit=0):
        env = dict(os.environ)
        env.update({
            "MVD_DATA_DIR": self.data_dir,
            "MVD_PROTOCOL": "jsonl",
        })
        proc = subprocess.run(
            [sys.executable, "-u", "-m", "minivectordb"],
            input=("".join(json.dumps(r, ensure_ascii=False) + "\n"
                           for r in requests)).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=env,
            timeout=180,
        )
        self.assertEqual(proc.returncode, expect_exit, proc.stderr.decode())
        lines = [
            line for line in proc.stdout.decode("utf-8").splitlines() if line
        ]
        return [json.loads(line) for line in lines]

    def setUp(self):
        import shutil
        import tempfile
        self.data_dir = tempfile.mkdtemp(prefix="mvd-jsonl-")
        self._cleanup = lambda: shutil.rmtree(self.data_dir, ignore_errors=True)

    def tearDown(self):
        self._cleanup()

    def test_handshake_is_exactly_the_ready_object(self):
        messages = self._run([{"id": 1, "op": "shutdown"}])
        self.assertEqual(messages[0], {"type": "ready"})

    def test_query_response_shape(self):
        messages = self._run([
            {"id": 1, "op": "execute",
             "sql": "CREATE TABLE docs (id BIGINT PRIMARY KEY, "
                    "content VARCHAR(16));", "search_mode": "exact"},
            {"id": 2, "op": "execute",
             "sql": "INSERT INTO docs VALUES (1, '你好');",
             "search_mode": "exact"},
            {"id": 3, "op": "execute",
             "sql": "SELECT id, content FROM docs ORDER BY id;",
             "search_mode": "exact"},
            {"id": 4, "op": "shutdown"},
        ])
        self.assertEqual(messages[0], {"type": "ready"})
        self.assertEqual(messages[1], {"id": 1, "ok": True})
        self.assertEqual(
            messages[2], {"id": 2, "ok": True, "affected_rows": 1}
        )
        self.assertEqual(
            messages[3],
            {"id": 3, "ok": True, "columns": ["id", "content"],
             "rows": [[1, "你好"]]},
        )
        self.assertEqual(messages[4], {"id": 4, "ok": True})

    def test_error_response_keeps_the_process_alive(self):
        messages = self._run([
            {"id": 1, "op": "execute",
             "sql": "CREATE TABLE d (id BIGINT PRIMARY KEY);",
             "search_mode": "exact"},
            {"id": 2, "op": "execute",
             "sql": "INSERT INTO d VALUES (1);", "search_mode": "exact"},
            {"id": 3, "op": "execute",
             "sql": "INSERT INTO d VALUES (1);", "search_mode": "exact"},
            {"id": 4, "op": "execute",
             "sql": "SELECT id FROM d;", "search_mode": "exact"},
            {"id": 5, "op": "shutdown"},
        ])
        self.assertIs(messages[3]["ok"], False)
        code = messages[3]["error"]["code"]
        self.assertIsInstance(code, str)
        self.assertTrue(code)
        # The statement after the failure still runs.
        self.assertIs(messages[4]["ok"], True)
        self.assertEqual(messages[4]["rows"], [[1]])

    def test_bigint_keeps_its_exact_value(self):
        big = 9007199254740993
        messages = self._run([
            {"id": 1, "op": "execute",
             "sql": "CREATE TABLE d (id BIGINT PRIMARY KEY, v BIGINT);",
             "search_mode": "exact"},
            {"id": 2, "op": "execute",
             "sql": "INSERT INTO d VALUES ({}, {});".format(big, -big),
             "search_mode": "exact"},
            {"id": 3, "op": "execute",
             "sql": "SELECT id, v FROM d;", "search_mode": "exact"},
            {"id": 4, "op": "shutdown"},
        ])
        rows = messages[3]["rows"]
        self.assertEqual(rows, [[big, -big]])
        # The value must be a JSON integer, not a string and not a float.
        self.assertIsInstance(rows[0][0], int)
        self.assertNotIsInstance(rows[0][0], float)

    def test_vector_projection_is_a_json_array(self):
        messages = self._run([
            {"id": 1, "op": "execute",
             "sql": "CREATE TABLE d (id BIGINT PRIMARY KEY, e VECTOR(2));",
             "search_mode": "exact"},
            {"id": 2, "op": "execute",
             "sql": "INSERT INTO d VALUES (1, '[3,4]');", "search_mode": "exact"},
            {"id": 3, "op": "execute",
             "sql": "SELECT id, e, L2_DISTANCE(e, '[0,0]') AS dist FROM d;",
             "search_mode": "exact"},
            {"id": 4, "op": "shutdown"},
        ])
        row = messages[3]["rows"][0]
        self.assertEqual(row[1], [3.0, 4.0])
        self.assertAlmostEqual(row[2], 5.0, places=6)

    def test_search_mode_switches_the_query(self):
        messages = self._run([
            {"id": 1, "op": "execute",
             "sql": "CREATE TABLE d (id BIGINT PRIMARY KEY, e VECTOR(2));",
             "search_mode": "exact"},
            {"id": 2, "op": "execute",
             "sql": "INSERT INTO d VALUES (1, '[0,0]'), (2, '[1,0]');",
             "search_mode": "exact"},
            {"id": 3, "op": "execute",
             "sql": "SELECT id FROM d ORDER BY L2_DISTANCE(e, '[0,0]') LIMIT 1;",
             "search_mode": "ann"},
            {"id": 4, "op": "shutdown"},
        ])
        self.assertEqual(messages[3]["rows"], [[1]])

    def test_unsupported_operation_is_reported(self):
        messages = self._run([
            {"id": 1, "op": "nonsense"},
            {"id": 2, "op": "shutdown"},
        ])
        self.assertIs(messages[1]["ok"], False)
        self.assertTrue(messages[1]["error"]["code"])

    def test_the_official_data_dir_variable_is_honoured(self):
        """The organisers' driver exports VECTORDB_DATA_DIR."""
        import shutil
        import tempfile
        other = tempfile.mkdtemp(prefix="mvd-alt-")
        try:
            env = dict(os.environ)
            env.update({
                "MVD_DATA_DIR": self.data_dir,
                "VECTORDB_DATA_DIR": other,
                "MVD_PROTOCOL": "jsonl",
            })
            proc = subprocess.run(
                [sys.executable, "-u", "-m", "minivectordb"],
                input=b'{"id":1,"op":"execute","sql":"CREATE TABLE x '
                      b'(id BIGINT PRIMARY KEY);","search_mode":"exact"}\n'
                      b'{"id":2,"op":"shutdown"}\n',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=ROOT, env=env, timeout=180,
            )
            self.assertEqual(proc.returncode, 0)
            # The table was created in the directory the driver named.
            self.assertTrue(os.listdir(other))
            self.assertEqual(os.listdir(self.data_dir), [])
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_nothing_is_written_after_the_shutdown_response(self):
        env = dict(os.environ)
        env.update({"MVD_DATA_DIR": self.data_dir, "MVD_PROTOCOL": "jsonl"})
        proc = subprocess.run(
            [sys.executable, "-u", "-m", "minivectordb"],
            input=b'{"id":1,"op":"shutdown"}\n',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=env,
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0)
        lines = proc.stdout.decode("utf-8").splitlines()
        # exactly the handshake and the shutdown response, nothing more
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0]), {"type": "ready"})
        self.assertEqual(json.loads(lines[1]), {"id": 1, "ok": True})


if __name__ == "__main__":
    unittest.main()
