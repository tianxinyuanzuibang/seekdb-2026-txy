# MiniVectorDB 加赛：9 次提测的完整排查记录

> 用途：交接文档。读完这一份，就掌握了本题 90% 的信息。
> 关键结论：**协议与语义已用官方工具验证通过（20/20），但正式评测仍未通过；
> 剩余失败点在隐藏的 9 个用例里，其断言内容官方不公开。**

---

## 一、结论速览

| 项 | 状态 | 证据 |
|---|---|---|
| JSONL 协议实现 | ✅ 正确 | 官方 `protocol_check/run.py` 20/20（`--image` 与 `--command` 两种模式），amd64 上同样 20/20 |
| 官方 20 个公开用例 | ✅ 全过 | 用官方自己的 `_assertions.py` 比较函数逐条比对，零失败 |
| 官方明列的 5 项隐藏要求 | ✅ 全过 | `tests/test_formal_requirements.py` 16 项断言全过 |
| BIGINT 精度 | ✅ 精确 | ±2^63 范围内 round-trip / WHERE / 排序 / 表达式均保持 JSON 整数 |
| 提交物完整性 | ✅ 一致 | 远端与本地 149 文件逐字节相同 |
| **正式评测** | ❌ **未通过** | 平台仅提示"失败用例数：2，原因：功能结果不符合要求" |

**已排除的可能**：协议错误、握手时序、错误码格式、列名渲染、大小写、NULL、
BIGINT 精度、Unicode 排序、LIMIT/OFFSET、affected_rows、事务语义、崩溃恢复、
幂等恢复、超内存数据、ANN 召回、请求大小上限、响应大小上限、镜像架构、
数据目录变量名、stdout 纯净性。

---

## 二、9 次提测的演进

| 次 | 版本要点 | 平台反馈 | 学到什么 |
|---|---|---|---|
| 1 | 纯文本协议（回显 SQL + tab 行） | 未通过（无细节） | 协议方向就错了 |
| 2 | 修 3 个阻塞 bug、加 `affected_rows`、`--data-dir` | 未通过（无细节） | 语义 bug 需靠官方用例反测 |
| 3 | 接入 20 个官方用例、支持 `--restart`/`--mode ann` | 未通过（无细节） | 官方用例是"自证"，不是"他证" |
| 4 | 修多行语句被切碎、指令边界吃字符 | 未通过（无细节） | 自建测试被样例形态带偏 |
| 5 | 改 jsonarray（stdout 一个 JSON 数组） | 未通过（无细节） | 仍非真实协议 |
| 6 | **改 JSONL（官方协议）** | 未通过（无细节） | 方向对了 |
| 7 | + `VECTORDB_DATA_DIR`、+ float32 溢出拒绝 | **"失败用例数：2"** | 协议层已通，只剩 2 个 |
| 8 | + 列名按定义大小写返回 | 0 分（无细节） | 平台本轮未展开细节 |
| 9 | + stderr 逐请求/响应诊断（`MVD_DIAG=1`） | 0 分（无细节） | 平台不展示日志 |

第 7 次的"2 个失败"是唯一的实质线索：**协议正确、29 个正式用例里 28 个通过**。

---

## 三、官方信息全貌（截至 2026-11-12）

官方分支 `oceanbase/seekdb@2026-competition-round2`，tip = `f4d4e26653af`（2026-11-10 03:30，"upload stdin/stdout protocol and one demo py to double check"），共 86 个文件：

```
STDIO_PROTOCOL.md          协议全文（311 行）
protocol_check/            官方自测驱动
  run.py                   --image / --command 两种模式
  _transport.py            Docker + JSONL 传输、握手校验、超时
  _assertions.py           比较语义（same() / assert_response()）
  config.json              2CPU / 2GiB / 128 PID / 超时 120s·60s·600s / 容差 1e-5
  example_stdio.py         通信骨架
mysqltest/                 20 个公开用例 / 95 条 SQL / JSON 断言
```

语雀《赛题一》最后更新 2026-11-09 08:49，此后无变化。

### 协议要点（逐字核对过）

- 启动：恢复完成后**主动**输出 `{"type":"ready"}` 并 flush，**必须在收到第一条请求之前**，每次重启都要再来一次
- 请求：`{"id":N,"op":"execute","sql":"...","search_mode":"exact|ann"}`
- 响应：`{"id":N,"ok":true}` / `+affected_rows` / `+columns,rows`；失败 `{"id":N,"ok":false,"error":{"code":"<非空字符串>","message":...}}`
- `id` 原样回传且保持 JSON 类型；`ok` 必须是 JSON 布尔
- `{"id":N,"op":"shutdown"}` → 回滚 + 落盘 + `{"id":N,"ok":true}` + 退出码 0，**之后 stdout 不得有任何输出**
- stdout 只有协议报文；日志走 stderr；每条响应后立即 flush
- 失败语句不终止进程
- 容器以 `--read-only --network=none --cap-drop=ALL` 运行，**只有 /data 可写**
- `--restart` / `--mode ann` 是**驱动指令**，不能当 SQL 发给数据库
- 官方 README 最后一句："本工具不会帮学生把 READY 或纯 SQL 协议自动转换成 JSON 协议"

### 比较语义（`_assertions.py`，最容易踩坑）

```python
def same(actual, expected, abs_tol=1e-5, rel_tol=1e-5):
    if isinstance(expected, float):
        return (type(actual) in (int, float) and math.isfinite(actual)
                and math.isclose(actual, expected, abs_tol=abs_tol, rel_tol=rel_tol))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(same(a, b, ...) for a, b in zip(actual, expected)))
    return type(actual) is type(expected) and actual == expected
```

- 期望是 float → 实际可以是 int 或 float，但必须有限
- 期望是 list → 实际必须是 list，**长度必须相等**
- 其它 → **`type(actual) is type(expected)`**，类型不同即失败（`1` ≠ `1.0` ≠ `"1"`）
- `unordered: true` → 多重集合比较，**重复行不可丢失**
- `error` 断言 → `ok` 必须 false，`error.code` 必须是非空字符串；`config.json` 的 `error_codes` 目前为 `{}`，故任何非空 code 都行

---

## 四、赛题明文要求但公开用例未覆盖的 5 项（已逐条实测）

| 要求 | 我们的实现 | 测试 |
|---|---|---|
| 拒绝 NaN / Infinity / 维度错误 / float32 溢出（INSERT、UPDATE SET、距离参数三处） | ✅ | `tests/test_formal_requirements.py` |
| 失败语句不部分生效；显式事务之前的成功修改仍可提交或回滚 | ✅ | 同上 |
| 已确认提交在强杀后保留；未提交修改不部分生效；恢复途中再次退出仍能恢复 | ✅ | 同上 |
| 能处理超过内存限额的数据 | ✅ 流式路径，2 万行×128 维峰值 31.8 MiB | `tests/test_recall.py`（缓存关闭时走流式路径） |
| ANN 满足过滤、有效 ID、无重复、召回率；索引更新不破坏事务语义 | ✅ recall 0.98–1.0 | `tests/test_recall.py` |

---

## 五、已修过的真实 bug（全部有回归测试）

| # | Bug | 后果 |
|---|---|---|
| 1 | `_expr_name` 函数头被性能重构覆盖，函数体成死代码 | 任何无别名投影 `NameError` |
| 2 | `ORDER BY <投影别名>` 解析不到 | 赛题示例的写法失败 |
| 3 | 引号向量 `'[0,2]'` 被拒 | 赛题示例+官方用例的写法 |
| 4 | **UPDATE 非原子** | 数据损坏级 |
| 5 | **向量 TopK 忽略 `ORDER BY DESC`** | 静默反向语义 |
| 6 | 无 LIMIT 的向量 TopK 返回空 | 官方用例全带 LIMIT，测不到 |
| 7 | `LIMIT 0` 崩溃（`max() empty`） | — |
| 8 | 事务内向量检索看不到自己的插入（存储层列扫描不合并 overlay） | 赛题明文要求 |
| 9 | 引号向量绕过维度 / float32 校验（tuple 快路径） | 官方明列要求 |
| 10 | 更新主键静默匹配 0 行 | 应报错 |
| 11 | 嵌套 BEGIN 被接受 | 应报错 |
| 12 | **多行语句被按行切碎执行**（圆括号未计数） | 多行 CREATE TABLE 建不出来 |
| 13 | `--mode ann` 指令被切掉首字符 | 模式切换失效 |
| 14 | 指令边界多消费一个字符 → 死循环 | — |
| 15 | `set_mode` 里 `executor` 未定义 → `NameError` | 遇指令即崩 |
| 16 | 向量缓存永不建立（鸡生蛋） | 查询 83ms（应 12ms），**性能分腰斩** |
| 17 | ANN 候选集请求数写成列数 | 结果缺行 |
| 18 | 缓存安装污染（ANN 子集当全表） | recall 塌到 0.15 |
| 19 | **裸列引用返回查询拼写而非定义名** | `columns` 断言失败 |
| 20 | 数据目录变量名不匹配（官方用 `VECTORDB_DATA_DIR`） | 数据写错地方 |

---

## 六、复现与验证

```bash
# 官方裁判（唯一权威）
python3 protocol_check/run.py --image <镜像> --output <新目录>     # 20/20
python3 protocol_check/run.py --command '["python3","-u","-m","minivectordb"]' \
        --output <新目录>                                            # 20/20

# 我们自己的回归
python3 -m unittest discover -s tests          # 218+
python3 tests/run_tests.py                     # mysqltest 风格用例
python3 tests/run_official_cases.py --array    # JSON 数组封装
python3 tests/test_formal_requirements.py      # 官方明列的隐藏要求
python3 tests/test_crash.py                    # SIGKILL 6 场景
python3 tests/test_recall.py --mode ann        # 召回率
python3 tests/test_streaming_memory.py         # 超内存
```

镜像必须在 **linux/amd64** 上验证（评测平台架构）。

---

## 七、给下一位接手的建议

1. **不要改语义**。语义层已用官方比较函数逐条验证，盲改风险大于收益。
2. **要诊断信息**：平台不展示日志，所以诊断只能靠"提测 + 问组织方"。
   可以合法地问组织方要**失败用例的 id 或失败类别**（赛题 3.5 承诺"返回失败类别和必要日志"）。
3. **优先想到的候选**（按可能性排序，均未验证）：
   - `error.code` 的**冻结映射**：正式平台的 `config.json` 可能已冻结具体拼写
     （`duplicate_key` / `unsupported_sql` / `no_such_table` / `invalid_value` …），
     我们目前用的是自选语义名。
   - **表达式列名**：我们渲染 `COUNT(*)`、`count(id)`、`v+1`；正式期望可能是
     `count(*)`、`count(id)`、`v+1` 的**原样小写**形式。
   - **`SELECT` 的表限定列**（`t.id`）返回的名字形态。
   - **距离的浮点表示**：我们返回 float32 舍入后的 double；官方可能期望 float64 计算结果。
   - **VARCHAR 长度与 Unicode 码点**：我们按码点计；官方 README 明确"不混入长度边界"，正式用例可能仍考。
4. **每次提测前跑一次官方裁判**，确认 20/20 再提交。
5. **不要再猜协议**。协议已经确定（JSONL），官方 `STDIO_PROTOCOL.md` 是唯一依据。

---

## 八、时间线与提交

| 日期 | 事件 |
|---|---|
| 11-09 07:00 | 官方发布第一批 5 个公开用例 |
| 11-09 18:40 | 官方发布第二批，扩到 20 个用例 + 驱动指令 + error 断言 |
| 11-10 03:30 | **官方发布 `STDIO_PROTOCOL.md` + `protocol_check/`（JSONL 协议）** |
| 11-10 | 我们实现 JSONL，官方裁判 20/20 |
| 11-11 | 平台首次给出细节："失败用例数：2" |
| 11-12 | 仍 0 分；平台不展示日志 |

仓库：`tianxinyuanzuibang/seekdb` 与 `tianxinyuanzuibang/seekdb-2026-txy`
分支：`2026-competition-round2`
