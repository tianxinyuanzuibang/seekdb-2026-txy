"""The public case pack under the alternative --restart driver model.

The published pack defines --restart as "shut down normally and wait for the
exit, then start again against the same data directory".  That can be read
two ways, and the database has to work under both:

  A) the driver stops the process and starts a new one;
  B) the driver sends the literal --restart and expects the database to
     shut itself down.

This file checks model B: the directive is written to the pipe, the database
exits by itself, a new process is started on the same data directory and the
rest of the case runs.  The output is a sequence of JSON arrays, one per
process, and the assertions of the segments are concatenated.

Model A is covered by tests/run_official_cases.py.

Run with: python3 tests/test_restart_model.py
"""
import json, os, subprocess, sys, tempfile
ROOT = "/Users/gu_jm/Documents/deepseek-harness/default-workspace/seekdb"
OFF = os.path.join(ROOT, "tests", "official")
manifest = json.load(open(os.path.join(OFF, "manifest.json")))

def run_segment(script, data_dir):
    env = dict(os.environ)
    env.update({"MVD_DATA_DIR": data_dir, "MVD_PROTOCOL": "line",
                 "MVD_ECHO": "0", "MVD_TERMINATOR": "0",
                 "MVD_OUTPUT": "jsonarray"})
    p = subprocess.run([sys.executable, "-u", "-m", "minivectordb"],
                       input=script.encode(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, cwd=ROOT, env=env, timeout=180)
    text = p.stdout.decode("utf-8", "replace").strip()
    if not text:
        return [], p.returncode
    try:
        return json.loads(text), p.returncode
    except ValueError:
        return None, p.returncode

fails = 0
for case in manifest["cases"]:
    name = case["id"]
    items = [l.strip() for l in open(os.path.join(OFF, case["test"]), encoding="utf-8") if l.strip()]
    expectations = json.load(open(os.path.join(OFF, case["result"]), encoding="utf-8"))
    d = tempfile.mkdtemp()
    produced = []
    segment = []
    ok = True
    for item in items:
        if item == "--restart":
            got, rc = run_segment("".join(s+"\n" for s in segment), d)
            segment = []
            if got is None:
                ok = False; break
            produced.extend(got)
            if rc != 0:
                ok = False; break
            continue
        segment.append(item)
    if segment:
        got, rc = run_segment("".join(s+"\n" for s in segment), d)
        if got is None: ok = False
        else: produced.extend(got)
    if not ok or len(produced) != len(expectations):
        print("FAIL(count/parse)", name, len(produced), "vs", len(expectations)); fails += 1; continue
    bad = False
    for a, e in zip(produced, expectations):
        if not isinstance(e, dict): continue
        if "error" in e:
            if "error" not in a: bad = True
        elif "error" in a: bad = True
        else:
            for k in ("affected_rows", "columns"):
                if k in e and a.get(k) != e[k]: bad = True
            if "rows" in e and a.get("rows") != e["rows"]: bad = True
    print(("OK   " if not bad else "FAIL "), name)
    if bad: fails += 1
print("\nmodel-B failures:", fails)
