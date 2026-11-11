"""BIGINT precision through every path the judge can compare.

The protocol requires BIGINT to stay a JSON integer: converting through a
float would lose precision beyond 2**53 and the judge compares the JSON
type as well as the value.

Run with: python3 tests/test_bigint_precision.py
"""
import json, os, subprocess, sys, tempfile
ROOT='/Users/gu_jm/Documents/deepseek-harness/default-workspace/seekdb'
def talk(reqs):
    dd=tempfile.mkdtemp()
    env=dict(os.environ); env.update({"MVD_DATA_DIR":dd,"MVD_PROTOCOL":"jsonl"})
    p=subprocess.Popen([sys.executable,"-u","-m","minivectordb"],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=ROOT,env=env)
    out,err=p.communicate(("\n".join(json.dumps(r,ensure_ascii=False) for r in reqs)+"\n").encode(),timeout=180)
    return [json.loads(l) for l in out.decode().splitlines() if l], err.decode()

fails=[]
def chk(n, ok, d=""):
    print(("OK   " if ok else "FAIL "), n, ("| "+str(d)[:150]) if not ok else "")
    if not ok: fails.append(n)

cases = [9007199254740991, 9007199254740992, 9007199254740993,
         2**53+1, 2**62, 2**63-1, -(2**62), 1234567890123456789]
for v in cases:
    m,e=talk([
      {"id":1,"op":"execute","sql":"CREATE TABLE p (id BIGINT PRIMARY KEY, v BIGINT);","search_mode":"exact"},
      {"id":2,"op":"execute","sql":"INSERT INTO p VALUES (%d, %d);"%(v, v),"search_mode":"exact"},
      {"id":3,"op":"execute","sql":"SELECT id, v FROM p;","search_mode":"exact"},
      {"id":4,"op":"execute","sql":"SELECT id FROM p WHERE v = %d;"%v,"search_mode":"exact"},
      {"id":5,"op":"execute","sql":"SELECT id FROM p WHERE id = %d;"%v,"search_mode":"exact"},
      {"id":6,"op":"shutdown"},
    ])
    rows = m[3].get("rows")
    ok = (m[2]["ok"] is True and rows == [[v, v]]
          and all(type(x) is int for x in rows[0])
          and m[4].get("rows") == [[v]] and m[5].get("rows") == [[v]])
    chk("BIGINT %d round-trip + WHERE"%v, ok,
        {"insert":m[2].get("error"),"rows":rows,"where_v":m[4].get("rows"),"where_id":m[5].get("rows")})

print("=== 排序中的 BIGINT (不能按浮点比较) ===")
m,e=talk([
 {"id":1,"op":"execute","sql":"CREATE TABLE o (id BIGINT PRIMARY KEY);","search_mode":"exact"},
 {"id":2,"op":"execute","sql":"INSERT INTO o VALUES (9007199254740993),(9007199254740991),(9007199254740992);","search_mode":"exact"},
 {"id":3,"op":"execute","sql":"SELECT id FROM o ORDER BY id;","search_mode":"exact"},
 {"id":4,"op":"shutdown"},
])
rows=[r[0] for r in m[3].get("rows",[])]
chk("排序保持整数精确序", rows==sorted(rows) and rows[0]==9007199254740991, rows)

print("=== 聚合/表达式里的 BIGINT ===")
m,e=talk([
 {"id":1,"op":"execute","sql":"CREATE TABLE g (id BIGINT PRIMARY KEY, v BIGINT);","search_mode":"exact"},
 {"id":2,"op":"execute","sql":"INSERT INTO g VALUES (1, 9007199254740993);","search_mode":"exact"},
 {"id":3,"op":"execute","sql":"SELECT id, v + 0 AS x, v * 1 AS y FROM g;","search_mode":"exact"},
 {"id":4,"op":"shutdown"},
])
rows=m[3].get("rows")
chk("表达式后仍是精确整数", rows==[[1, 9007199254740993, 9007199254740993]], rows)
if rows and rows[0][1] != 9007199254740993:
    print("   注意: 实际 %r (类型 %s)" % (rows[0][1], type(rows[0][1]).__name__))

print("\nFAILURES:", fails if fails else "none")
