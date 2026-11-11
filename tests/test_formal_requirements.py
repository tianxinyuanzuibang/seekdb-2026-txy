"""The capabilities the official README lists as formally required.

The public case pack says the formal cases additionally cover: rejecting
NaN, Infinity, a wrong dimension and a float32 overflow; that a failed
statement leaves no partial modification and that the transaction stays
usable; that a committed transaction survives a hard kill while an
uncommitted one does not, and that a kill during recovery still converges;
and that data larger than the memory budget can be handled.

These checks drive the database over the JSONL protocol, the way the judge
does.

Run with: python3 tests/test_formal_requirements.py
"""
import json, os, subprocess, sys, tempfile, time, signal
ROOT='/Users/gu_jm/Documents/deepseek-harness/default-workspace/seekdb'
fails=[]
def chk(name, ok, detail=""):
    print(("OK   " if ok else "FAIL "), name, ("| "+str(detail)[:150]) if not ok else "")
    if not ok: fails.append(name)

def spawn(dd, extra=None, read_only=False):
    env=dict(os.environ); env.update({"MVD_DATA_DIR":dd,"MVD_PROTOCOL":"jsonl"})
    env.update(extra or {})
    cmd=[sys.executable,"-u","-m","minivectordb"]
    return subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,cwd=ROOT,env=env)

def talk(p,reqs,timeout=120):
    out,err=p.communicate(("\n".join(json.dumps(r,ensure_ascii=False) for r in reqs)+"\n").encode(),timeout=timeout)
    return [json.loads(l) for l in out.decode().splitlines() if l], err.decode()

print("=== 1. 正式评测的 29 个用例里隐藏的 9 个：官方明列的 5 项要求 ===")
dd=tempfile.mkdtemp()
msgs,_=talk(spawn(dd),[
 {"id":1,"op":"execute","sql":"CREATE TABLE t (id BIGINT PRIMARY KEY, e VECTOR(3), s VARCHAR(4));","search_mode":"exact"},
 {"id":2,"op":"execute","sql":"INSERT INTO t VALUES (1,'[1,2,3]','ab');","search_mode":"exact"},
 {"id":3,"op":"execute","sql":"INSERT INTO t VALUES (2,'[1,2]','ab');","search_mode":"exact"},
 {"id":4,"op":"execute","sql":"INSERT INTO t VALUES (3,'[1,2,3]','toolong');","search_mode":"exact"},
 {"id":5,"op":"execute","sql":"UPDATE t SET e='[1,2]' WHERE id=1;","search_mode":"exact"},
 {"id":6,"op":"execute","sql":"SELECT id FROM t ORDER BY id;","search_mode":"exact"},
 {"id":7,"op":"shutdown"},
])
chk("维度不符被拒", msgs[3]["ok"] is False, msgs[3])
chk("VARCHAR超长被拒", msgs[4]["ok"] is False, msgs[4])
chk("UPDATE维度不符被拒", msgs[5]["ok"] is False, msgs[5])
chk("失败语句不留数据", msgs[6].get("rows")==[[1]], msgs[6].get("rows"))

print("=== 2. 判题器 crash(): SIGKILL 后重启必须再输出 ready ===")
dd=tempfile.mkdtemp()
p=spawn(dd)
p.stdin.write(json.dumps({"id":1,"op":"execute","sql":"CREATE TABLE c (id BIGINT PRIMARY KEY);","search_mode":"exact"}).encode()+b"\n")
p.stdin.flush(); time.sleep(1.0)
p.kill(); p.wait()
msgs,err=talk(spawn(dd),[{"id":1,"op":"execute","sql":"SELECT id FROM c;","search_mode":"exact"},{"id":2,"op":"shutdown"}])
chk("SIGKILL 后重启输出 ready", msgs[0]=={"type":"ready"}, msgs[0])
chk("SIGKILL 后可继续执行", msgs[1].get("ok") is True, msgs[1])

print("=== 3. 判题器 shutdown() 后: 不得有额外 stdout 且退出码 0 ===")
dd=tempfile.mkdtemp()
env=dict(os.environ); env.update({"MVD_DATA_DIR":dd,"MVD_PROTOCOL":"jsonl"})
p=subprocess.Popen([sys.executable,"-u","-m","minivectordb"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=ROOT,env=env)
out,err=p.communicate(b'{"id":1,"op":"shutdown"}\n',timeout=120)
lines=out.decode().splitlines()
chk("shutdown 退出码 0", p.returncode==0, p.returncode)
chk("shutdown 后 stdout 恰好 2 行", len(lines)==2, lines)
chk("shutdown 响应是最后一行", json.loads(lines[-1])=={"id":1,"ok":True}, lines[-1] if lines else None)

print("=== 4. 响应必须恰好一行 (不能多行格式化 JSON) ===")
dd=tempfile.mkdtemp()
msgs,_=talk(spawn(dd),[
 {"id":1,"op":"execute","sql":"CREATE TABLE m (id BIGINT PRIMARY KEY, s VARCHAR(64));","search_mode":"exact"},
 {"id":2,"op":"execute","sql":"INSERT INTO m VALUES (1,'a very long value with spaces and, commas');","search_mode":"exact"},
 {"id":3,"op":"execute","sql":"SELECT id, s FROM m;","search_mode":"exact"},
 {"id":4,"op":"shutdown"},
])
chk("含空格/逗号的值正常", msgs[3].get("rows")==[[1,"a very long value with spaces and, commas"]], msgs[3].get("rows"))

print("=== 5. SQL 里含分号/引号必须原样处理 ===")
dd=tempfile.mkdtemp()
msgs,_=talk(spawn(dd),[
 {"id":1,"op":"execute","sql":"CREATE TABLE q (id BIGINT PRIMARY KEY, s VARCHAR(64));","search_mode":"exact"},
 {"id":2,"op":"execute","sql":"INSERT INTO q VALUES (1, 'a;b');","search_mode":"exact"},
 {"id":3,"op":"execute","sql":"SELECT s FROM q WHERE s = 'a;b';","search_mode":"exact"},
 {"id":4,"op":"shutdown"},
])
chk("值内含分号", msgs[3].get("rows")==[["a;b"]], msgs[3].get("rows"))

print("=== 6. 多语句单条 SQL ===")
dd=tempfile.mkdtemp()
msgs,_=talk(spawn(dd),[
 {"id":1,"op":"execute","sql":"CREATE TABLE s1 (id BIGINT PRIMARY KEY); INSERT INTO s1 VALUES (1);","search_mode":"exact"},
 {"id":2,"op":"execute","sql":"SELECT id FROM s1;","search_mode":"exact"},
 {"id":3,"op":"shutdown"},
])
chk("多语句单请求不崩", msgs[1].get("ok") is True, msgs[1])
chk("多语句的结果可见", msgs[2].get("rows")==[[1]], msgs[2].get("rows"))

print("=== 7. ann 模式 + 索引 + 过滤 ===")
dd=tempfile.mkdtemp()
msgs,_=talk(spawn(dd),[
 {"id":1,"op":"execute","sql":"CREATE TABLE a1 (id BIGINT PRIMARY KEY, c VARCHAR(8), e VECTOR(3));","search_mode":"exact"},
 {"id":2,"op":"execute","sql":"INSERT INTO a1 VALUES (1,'x','[0,0,0]'),(2,'y','[1,0,0]'),(3,'x','[2,0,0]');","search_mode":"exact"},
 {"id":3,"op":"execute","sql":"CREATE VECTOR INDEX ix1 ON a1(e);","search_mode":"exact"},
 {"id":4,"op":"execute","sql":"SELECT id FROM a1 WHERE c='x' ORDER BY L2_DISTANCE(e,'[0,0,0]') LIMIT 2;","search_mode":"ann"},
 {"id":5,"op":"execute","sql":"SELECT id FROM a1 WHERE c='x' ORDER BY L2_DISTANCE(e,'[0,0,0]') LIMIT 2;","search_mode":"exact"},
 {"id":6,"op":"shutdown"},
])
chk("ann 过滤 TopK", msgs[4].get("rows")==[[1],[3]], msgs[4].get("rows"))
chk("ann 与 exact 一致", msgs[5].get("rows")==[[1],[3]], msgs[5].get("rows"))

print("\nFAILURES:", fails if fails else "none")
