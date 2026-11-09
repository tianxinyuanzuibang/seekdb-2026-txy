#!/usr/bin/env python3
"""Crash-recovery checks for MiniVectorDB.

The formal tests kill the process (SIGKILL) and restart it against the
same data directory.  These checks reproduce that:

* a committed transaction must survive a hard kill,
* an uncommitted transaction must not be visible after the restart,
* killing the process *during* recovery and restarting again must reach
  exactly the same state (recovery is idempotent).

    python3 tests/test_crash.py
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SENTINEL = "--MVD-END--"


def start(data_dir):
    env = dict(os.environ)
    env["MVD_DATA_DIR"] = data_dir
    env["MVD_PROTOCOL"] = "line"
    env["MVD_ECHO"] = "0"
    env["MVD_RESULT_SENTINEL"] = SENTINEL
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "minivectordb"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=ROOT,
        env=env,
    )


def query(proc, sql):
    """Run a statement and return its rows (without the header line)."""
    rows = query_raw(proc, sql)
    return rows[1:] if rows else rows


def query_raw(proc, sql):
    proc.stdin.write((sql + "\n").encode("utf-8"))
    proc.stdin.flush()
    rows = []
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("database closed the connection")
        text = line.decode("utf-8").rstrip("\n")
        if text == SENTINEL:
            return rows
        if text.startswith("ERROR"):
            raise RuntimeError(text)
        rows.append(text.split("\t"))


def kill(proc):
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)


def check(name, condition, detail=""):
    status = "OK  " if condition else "FAIL"
    print("[ {} ] {}{}".format(status, name, (" - " + detail) if detail else ""))
    return bool(condition)


def scenario_committed_survives(data_dir):
    proc = start(data_dir)
    query(proc, "create table t (id bigint, v bigint, primary key (id))")
    query(proc, "insert into t values (1, 10)")
    query(proc, "begin")
    query(proc, "insert into t values (2, 20)")
    query(proc, "commit")
    kill(proc)

    proc = start(data_dir)
    rows = query(proc, "select id, v from t order by id")
    proc.stdin.close()
    proc.wait(timeout=30)
    ok = rows == [["1", "10"], ["2", "20"]]
    return check("committed data survives SIGKILL", ok, str(rows))


def scenario_uncommitted_lost(data_dir):
    proc = start(data_dir)
    query(proc, "create table t (id bigint, v bigint, primary key (id))")
    query(proc, "insert into t values (1, 10)")
    query(proc, "begin")
    query(proc, "insert into t values (2, 20)")
    # No commit: the process is killed with the transaction open.
    kill(proc)

    proc = start(data_dir)
    rows = query(proc, "select id, v from t order by id")
    proc.stdin.close()
    proc.wait(timeout=30)
    ok = rows == [["1", "10"]]
    return check("uncommitted transaction is not applied", ok, str(rows))


def scenario_rollback_then_kill(data_dir):
    proc = start(data_dir)
    query(proc, "create table t (id bigint, v bigint, primary key (id))")
    query(proc, "insert into t values (1, 10)")
    query(proc, "begin")
    query(proc, "insert into t values (2, 20)")
    query(proc, "rollback")
    kill(proc)

    proc = start(data_dir)
    rows = query(proc, "select id, v from t order by id")
    proc.stdin.close()
    proc.wait(timeout=30)
    ok = rows == [["1", "10"]]
    return check("rolled back data stays rolled back", ok, str(rows))


def scenario_repeated_kill_during_recovery(data_dir):
    """Kill the process while it is starting up, repeatedly."""
    proc = start(data_dir)
    query(proc, "create table t (id bigint, v bigint, primary key (id))")
    for i in range(1, 21):
        query(proc, "insert into t values ({}, {})".format(i, i * 100))
    kill(proc)

    # Now kill several times during the recovery itself.
    for _ in range(5):
        proc = start(data_dir)
        time.sleep(0.02)
        kill(proc)

    proc = start(data_dir)
    rows = query(proc, "select id, v from t order by id")
    proc.stdin.close()
    proc.wait(timeout=30)
    ok = len(rows) == 20 and rows[0] == ["1", "100"] and rows[-1] == ["20", "2000"]
    return check("repeated kills during recovery are idempotent", ok,
                 "{} rows".format(len(rows)))


def scenario_clean_restart(data_dir):
    proc = start(data_dir)
    query(proc, "create table t (id bigint, v bigint, primary key (id))")
    query(proc, "insert into t values (1, 10)")
    query(proc, "insert into t values (2, 20)")
    proc.stdin.close()
    proc.wait(timeout=30)

    proc = start(data_dir)
    rows = query(proc, "select id, v from t order by id")
    proc.stdin.close()
    proc.wait(timeout=30)
    ok = rows == [["1", "10"], ["2", "20"]]
    return check("clean restart keeps the data", ok, str(rows))


def scenario_rollback_restores_exactly(data_dir):
    proc = start(data_dir)
    query(proc, "create table t (id bigint, v bigint, primary key (id))")
    query(proc, "insert into t values (1, 10)")
    query(proc, "insert into t values (2, 20)")
    query(proc, "begin")
    query(proc, "update t set v = 999 where id = 1")
    query(proc, "delete from t where id = 2")
    query(proc, "insert into t values (3, 30)")
    rows = query(proc, "select id, v from t order by id")
    query(proc, "rollback")
    rows = query(proc, "select id, v from t order by id")
    proc.stdin.close()
    proc.wait(timeout=30)
    ok = rows == [["1", "10"], ["2", "20"]]
    return check("rollback restores the pre-transaction state", ok, str(rows))


def main():
    results = []
    for scenario in (
        scenario_committed_survives,
        scenario_uncommitted_lost,
        scenario_rollback_then_kill,
        scenario_clean_restart,
        scenario_rollback_restores_exactly,
        scenario_repeated_kill_during_recovery,
    ):
        directory = tempfile.mkdtemp(prefix="mvd-crash-")
        try:
            results.append(scenario(directory))
        finally:
            shutil.rmtree(directory, ignore_errors=True)
    print("\n{} scenario(s), {} failure(s)".format(
        len(results), results.count(False)
    ))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
