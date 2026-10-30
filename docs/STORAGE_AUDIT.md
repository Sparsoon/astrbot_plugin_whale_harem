# 存储层审查与运维（A1）

> **为什么在 2G 机器上要单独做这份审查**（6 行摘要）：
> 1. 存储层是唯一会"越跑越慢"的模块：漏还连接、WAL 不 checkpoint、索引缺失，
>    在 2 核机器上都会放大成"机器人偶尔卡死"，而且**不会报错**，只能靠审计发现；
> 2. 本次只做**兼容性加固**（异常处理 / 边界值 / 回滚），不改任何公开方法名与参数，
>    其他 4 个 Agent 正在并行调用 `Repo`，签名一变就是集体返工；
> 3. `schema.py` 与 `sql/schema.sql` 属队长/我各自的交付物，结构类改动一律走"建议"而不是偷改；
> 4. 加固目标不是"更优雅"，而是**把可恢复的错误变成可重试的业务异常**，
>    让指令层能回一张提示卡，而不是让用户等到超时；
> 5. 2G 机器上运维手段必须"零常驻成本"：不做定时备份任务、不做定时 VACUUM，
>    全部是低频手工/启动时一次性动作。

审查范围：`core/db/database.py`、`core/db/repos.py`、`core/db/schema.py`、
`sql/schema.sql`、`core/models.py` 的 JSON 解析路径。
审查方式：逐行静态推演（本环境无 Python/shell 执行器），并发场景按
"5 连接 × 200 并发 × 单写者（BEGIN IMMEDIATE）"推演。

---

## 1. 结论摘要

| 项 | 结论 |
|---|---|
| 公开方法签名 | **零改动**（`Repo` 全部方法与 `Database` 全部方法名/参数保持原样） |
| 已有行为语义 | **零改动**（返回值、SQL 语义、事务边界、数据格式不变） |
| 修复 | 13 处（全部是异常路径 / 边界值 / 资源释放） |
| 未修但记录 | 9 条已知问题/建议（见 §4，其中 3 条需要队长决策） |
| `sql/schema.sql` 与 `SCHEMA_SQL` | DDL 逐字一致；额外只有注释、PRAGMA 建议、文件头 JSON 说明 |
| 第三方依赖 | 未新增（仅新增 `import sqlite3` 用于异常类型判断，不做任何同步调用） |
| 连接池上限 | 仍为 5；内存估算 ~12–14MB（见 `DB_DESIGN.md` §7） |

---

## 2. 已修复（兼容性加固明细）

标记：**D** = `database.py`，**R** = `repos.py`。全部只影响"出错时会怎样"，
正常路径的 SQL 文本与参数顺序未变（`add_coins` 除外：见 R-3，SQL 相同，只是并入同一事务）。

### 2.1 database.py

| # | 问题（原状） | 加固后 | 影响面 |
|---|---|---|---|
| D-1 | `init()` 中途失败（磁盘满 / 权限 / PRAGMA 异常）→ 已打开的连接既不关闭也不回队列；`self._conns` 非空导致**下次 `init()` 直接 return**，池子永久残缺 | `try/except BaseException: await self._dispose_partial(); raise`；`_dispose_partial` 关连接 + 重建队列 | 只在建库失败时触发；失败后重试 `init()` 能正常工作 |
| D-2 | `async with transaction(): executescript(SCHEMA_SQL)`：`executescript` 在 legacy 事务模式下会**先隐式 COMMIT**，白抢一次写锁，语义也误导 | 改为 `async with connection(): executescript(...)`；建表靠 `IF NOT EXISTS` 幂等 | 启动快一丁点；不再有"事务里其实已提交"的错觉 |
| D-3 | `connection()` 里任何 `sqlite3.OperationalError`（含 `database is locked`、`disk I/O error`）原样冒到 service → 指令层视作未知异常 → **用户没有回复** | 捕获后先回滚，命中 `locked/busy/disk i/o` 三类瞬时错误 → 抛 `DBPoolTimeout("数据库繁忙，请稍后再试")`（`WhaleError` 子类，`safe_handler` 会转错误卡）；其它 OperationalError（如 SQL 写错）**照原样抛**，不掩盖 bug | 抖动时用户看到"稍后重试"而不是静默；SQL 错误仍会进日志暴露 |
| D-4 | `BEGIN IMMEDIATE` 失败没有分类；`conn.commit()` 失败时**没有回滚**，脏连接被放回池子，下一个使用者会读到未提交事务 | BEGIN 失败同样区分瞬时/非瞬时；commit 失败先回滚再抛 | 极端 IO 错误下不再污染池子 |
| D-5 | `vacuum_if_needed` 只 `except aiosqlite.Error`，D-3 之后锁超时会变成 `DBPoolTimeout` 逃出去 | 改为 `except (aiosqlite.Error, DBPoolTimeout)` → 返回 False + warning | 运维动作失败不再影响主流程 |

### 2.2 repos.py

| # | 问题（原状） | 加固后 | 影响面 |
|---|---|---|---|
| R-1 | `get_user(uid, name=None)`：`name=None` 写 `NOT NULL` 列 → `IntegrityError`；`uid=None` 时 `INSERT` 后 `SELECT ... WHERE uid=NULL` 永远查不到 → `User.from_row(None)` 抛 `ValueError`（不是 WhaleError，指令层无提示） | `uid/name` 先归一为 `str`；仍取不到行则 `log.error` + `raise WhaleError("用户数据读写出错，请稍后重试")` | 正常调用完全不变；脏入参变成可展示的业务错误 |
| R-2 | `update_user(**fields)` 传 `None` → `IntegrityError` 打回整个事务 | 过滤 `None` 值（所有列都是 NOT NULL，写 None 本来就是错的），并 debug 记录丢弃数量 | 正常调用不变 |
| R-3 | `add_coins`：`UPDATE` 与随后的 `SELECT` 走**两条连接/两个事务**，返回值可能与写入不一致；uid 不存在时 `UPDATE` 影响 0 行，金币"加了等于没加"且**无任何日志** | `UPDATE + SELECT` 合并进同一个 `BEGIN IMMEDIATE` 事务；`rowcount==0` 时 warning；`delta` 非 int 时归一为 0 并 warning | 返回的余额一定等于本次修改后；SQL 文本未变 |
| R-4 | `spend_coins/add_exp`：`amount` 传 `"5"`/`5.9` 之类会污染 INTEGER 列或抛 `TypeError` | `int()` 归一；非法值分别按"扣费失败"与"经验 0"处理并记录 | 正常调用不变 |
| R-5 | `get_bag/mutate_bag`：`row["items"]` 在列缺失（旧库/手工改表）时抛 `IndexError`，直接炸到指令层 | 新增 `_bag_from_row()`：列缺失/JSON 损坏退化为空 `Bag` + warning（mutate 后会把正确的 JSON 写回，**自愈**） | 只在结构不一致时触发 |
| R-6 | `add_items/consume_items/donate_materials`：`int(num)` 遇到 `None`/`"abc"` 抛 `ValueError`，整个事务回滚，用户侧表现为"操作没反应" | 新增 `_clean_items()` 预清洗（丢 None/非数字/非正数）；`donate_materials` 的 `int(num)` 循环一并替换 | 脏入参被跳过而不是炸掉；合法入参结果等价 |
| R-7 | `ensure_harem` 抛 `RuntimeError`（非 WhaleError → 无提示卡）；`mutate_harem` 在 `INSERT OR IGNORE` 后仍取不到行时，会用**空行**继续 mutate，最终 `UPDATE ... WHERE target_uid=''` 影响 0 行 → 关系数据静默丢失 | 两处都改为 `raise WhaleError("关系数据初始化失败，请稍后重试")` + error 日志；mutate 前显式判空 | 只在表被删/只读盘等极端情况触发 |
| R-8 | `add_exp` 未命中用户时静默返回 `(1,0,False)` | 加 warning（不改返回） | 纯日志 |

> 未改的：所有 SQL 文本（除 R-3 的事务包裹）、所有方法名、参数名、默认值、返回结构、
> `mutate_bag`/`mutate_harem` 的回调契约（同步函数、返回值忽略）。

---

## 3. 一致性自检（`sql/schema.sql` vs `schema.py`）

`sql/schema.sql` 里 6 个 `CREATE TABLE` / 3 个 `CREATE INDEX` 的语句体是从
`core/db/schema.py::SCHEMA_SQL` **逐字复制**的（表名、列名、列顺序、类型、`NOT NULL`、
`DEFAULT`、复合主键、索引名与列序完全一致）。额外内容只有三类，均不影响执行结果：

1. 文件头注释（2G 设计说明 + 每个 JSON 字段的结构示例）；
2. 一段"建议的 PRAGMA"（与 `database.py::_PRAGMAS` 一致，供 DBA 手工建库时使用）；
3. 末尾 `PRAGMA user_version=1;`（与 `SCHEMA_VERSION` 一致）。

需要复核时，可在有 Python 的机器上跑这段（只读，不改任何文件）：

```python
# python -c "..."（思路：把两个文件的 DDL 行归一化后比对）
import re, pathlib
py = pathlib.Path("core/db/schema.py").read_text(encoding="utf-8")
sql = pathlib.Path("sql/schema.sql").read_text(encoding="utf-8")
def ddl(text):
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("--") or s.upper().startswith("PRAGMA"):
            continue
        keep.append(" ".join(s.split()))
    return "\n".join(keep)
body_py = ddl(py.split('SCHEMA_SQL = """', 1)[1].split('"""', 1)[0])
print("一致" if body_py == ddl(sql) else "不一致，请逐行比对")
```

> 注意：`schema.py` 是运行期唯一真源；`sql/schema.sql` 只用于审阅与手工建库。
> 任何结构改动都必须**先改 `schema.py`**，再同步本文件（顺序反了会导致线上库与代码漂移）。

---

## 4. 未修 / 已知问题（记录在案）

| # | 问题 | 严重度 | 处理建议 |
|---|---|---|---|
| K-1 | `public_targets`（`ORDER BY chase_count DESC`）与 `ntr_ranking`（`ORDER BY ntr_count DESC`）**没有对应索引**，退化为"扫本群切片" | 低（≤500 行，<1ms） | 数据量涨 10 倍再加索引（DDL 见 `DB_DESIGN.md` §4）。**必须由队长改 `schema.py`**，我无权改而且会破坏两文件一致性 |
| K-2 | `delete_all_of_user` 最后一条 `UPDATE harem SET owner_uid='' ... WHERE owner_uid=?` **永远匹配 0 行**（前一句已把同样的行 DELETE 掉） | 极低（死 SQL，无副作用） | 如果产品口径是"擦除此人但不破坏他人后宫"，应把 UPDATE 移到 DELETE 之前；如果口径是"连同关系一起删"，删掉这条 UPDATE 即可。**需队长确认口径**，我按"不改语义"原则未动 |
| K-3 | 同一问题：`delete_all_of_user` **不处理** `target_uid=uid` 的行（此人作为目标的关系记录仍保留 `target_name`） | 低（合规口径问题） | 若按 GDPR 严格擦除，应额外 `UPDATE harem SET target_name='' WHERE target_uid=?`；需队长决策 |
| K-4 | `save_harem` 是单条 `UPDATE`，行不存在时静默 no-op（`db.execute` 拿不到 rowcount） | 低 | 约定：`save_harem` 只在 `ensure_harem` / `mutate_harem` 之后调用；service 层不要自己 new `HaremRow` 直接存 |
| K-5 | `get_user` 的 `SELECT → INSERT OR IGNORE → SELECT` 三步不在同一事务 | 极低 | 并发下靠 `INSERT OR IGNORE` 兜底，结果正确，只是多一次往返。合并进事务会给**读**加写锁，得不偿失，故不改 |
| K-6 | `affection_ranking` 的 `owner_uid<>''` 会让 `idx_harem_owner` 的第二列退化为扫描 | 低 | 只扫本群切片（≤500 行），可接受；真要优化就加部分索引 `WHERE owner_uid<>''`（SQLite 支持），留给后续 |
| K-7 | `parse_bag` 只保留 fish 列表**尾部** `limit` 条 | 中（调配置时） | 若把 `fish_bag_limit` 调小，下次读背包会静默丢掉最旧的鱼（本来也该被卖/折算）。**提醒 A3/队长**：调小上限前先让玩家卖鱼，或在管理面板里提示 |
| K-8 | `add_coins` 允许负数把余额扣成负（契约说负数走 `spend_coins`） | 低 | 属调用约定而非数据库约束；不加 `CHECK(coins>=0)`，避免改变经济语义与升级路径 |
| K-9 | `buffs.payload` 用 `parse_int_dict` 读取，**只保留 int 值**；而 `services/items.py` 写入了 `{"src": item.id, "by": uid}`（字符串），读回来时这两个键会消失 | 低（当前无人依赖，全仓只判断 `payload is not None`） | 若将来要按 payload 判断"谁给谁挂的护盾"，需把 `get_buff` 改成 `json.loads` + 保留 str 值（会改变返回结构，需队长批准）；否则建议 items.py 只传 int payload，避免"写了读不到"的误解 |

---

## 5. 2G 机器「数据库运维 checklist」

> 全部命令在服务器上以插件数据目录为工作目录执行；`whale.db` 的默认位置是
> `<AstrBot 数据目录>/plugin_data/astrbot_plugin_whale_harem/whale.db`（见 `core/paths.py`）。
> 下面的 `sqlite3` 指系统 sqlite3 CLI（可选装，其它模块不依赖它）。

### 5.1 日常（每周 1 次，2 分钟）

- [ ] 看体积：`ls -lh whale.db*` —— 主库 < 30MB、`-wal` < 4MB 属正常；
- [ ] 看空闲页：`sqlite3 whale.db "PRAGMA freelist_count; PRAGMA page_count;"` ——
      `freelist/page_count > 0.25` 才考虑 VACUUM；
- [ ] 看日志：`grep -E "DBPoolTimeout|数据库抖动|鲸鱼馆数据库" astrbot.log | tail -50` ——
      出现 "数据库抖动" 说明有锁竞争或 IO 抖动，先查"事务里有没有慢 await"；
- [ ] 完整性抽检：`sqlite3 whale.db "PRAGMA quick_check;"` —— 期望输出 `ok`。

### 5.2 备份（每天 1 次，建议 04:05 之后，即游戏日切换后）

- [ ] 在线备份（**推荐**，不锁库、不丢 WAL）：
      `sqlite3 whale.db ".backup '/path/backup/whale-$(date +%F).db'"`
- [ ] 校验备份：`sqlite3 /path/backup/whale-XXXX.db "PRAGMA integrity_check;"` → `ok`
- [ ] 保留 7 份后删除最旧（40G 盘足够，但别无限堆）；
- [ ] **禁止**在运行中直接 `cp whale.db`（会漏掉 `-wal` 里已提交但未回写的数据）；
      停机窗口内拷贝则必须 `whale.db` + `whale.db-wal` + `whale.db-shm` **三个一起拷**。

### 5.3 WAL 截断

- [ ] 正常停机：`Database.close()` 已在每条连接上执行 `PRAGMA wal_checkpoint(TRUNCATE)`，
      退出后 `-wal` 应为 0 字节或消失；
- [ ] 运行期发现 `-wal` 异常大（>10MB）：
      1. 先 `sqlite3 whale.db "PRAGMA wal_checkpoint(PASSIVE);"` 看返回（第三列 `busy=1` = 有活跃读者）；
      2. 若 busy：说明有长读事务/长写事务 —— **优先排查代码**（铁律：事务里不许 `await` LLM/渲染/网络）；
      3. 允许短暂影响时用 `PRAGMA wal_checkpoint(TRUNCATE);`（需要无活跃读者）；
      4. 兜底：低峰期重启插件（`close()` 会截断）。
- [ ] 切勿手工删除运行中的 `-wal`（会丢已提交数据并可能损坏库）；`-shm` 可以删（重启后重建）。

### 5.4 损坏恢复（按顺序尝试，先备份现场）

- [ ] 症状：日志出现 `database disk image is malformed` / `file is not a database`；
- [ ] 第 1 步：立刻停止插件，把 `whale.db*` **整个目录**复制到安全位置（保留证据）；
- [ ] 第 2 步：`sqlite3 whale.db "PRAGMA integrity_check;"` 确认损坏范围；
- [ ] 第 3 步（首选）：用最近一份 `.backup` 备份恢复 —— 损失不超过 24h 的玩法数据；
- [ ] 第 4 步（想尽量抢救）：`sqlite3 whale.db ".recover" | sqlite3 whale-new.db`（sqlite ≥3.29），
      跑 `integrity_check` 通过后替换；替换前确保 `-wal`/`-shm` 已删除；
- [ ] 第 5 步（最坏）：删库重建 —— 表结构由 `SCHEMA_SQL` 自动创建，
      代价是所有玩家的鲸币/后宫清零，必须由队长决定；
- [ ] 事后：把 `journal_mode` / 磁盘空间 / 异常断电记录写进复盘（`disk I/O error` 多半是盘满或盘坏）。

### 5.5 Prune 策略（不需要任何定时任务）

| 数据 | 增长源 | 现有抑制机制 | 手工动作（可选） |
|---|---|---|---|
| 鱼护 `bags.fish` | 每次钓鱼 +1 条 | `Bag.add_fish` 按 `fish_bag_limit`（默认 80 / 硬上限 200）**裁掉最旧的**，溢出由 service 折成鲸币 | 无（调小上限前先让玩家卖鱼，见 K-7） |
| `bags.items` | 物品堆叠 | 单物品 `ITEM_STACK_LIMIT=9999`，扣到 0 删键 | 无 |
| `buffs` | 体验券 / 护盾 | `get_buff` 读到过期就地 DELETE；`expire_buffs()` 启动清一次 | 无需定时；手工 `DELETE FROM buffs WHERE expires_ts<=strftime('%s','now');` |
| `harem` | 被追求/被娶的目标 | 行数 ≤ 群人数（复合主键天然去重） | 退群用户的行会留着（展示为"已退群"）；如需清理按活跃名单手工删 |
| `users` | 每个交互过的人 | 一人一行，没有历史表 | 超长期不活跃可按 `updated_at` 手工清理（**需产品确认**） |
| `kv` | 杂项小状态 | 应保持两位数行 | 定期看一眼 `SELECT k, length(v) FROM kv ORDER BY length(v) DESC LIMIT 10;` |
| 空闲页 | DELETE / UPDATE 变长字段 | 无自动机制 | `freelist/page_count > 0.25` 或文件 >48MB 时低峰期 `VACUUM;`（需 ~2× 磁盘，持写锁数秒） |

**明确不做的事**（做了就是给 2 核机器上刑）：
* 不做每日定时 VACUUM / 定时 checkpoint 任务；
* 不做每小时"清理过期数据"的轮询协程（一切靠惰性判断 + 启动时一次性清理）；
* 不把 DB 放在 tmpfs / 内存盘（2G 内存扛不住，且断电全丢）；
* 不和 NapCat 的图片缓存目录混放（避免它清理缓存时误删 DB 文件）。

### 5.6 观测指标与红线

| 指标 | 正常 | 红线 | 处置 |
|---|---|---|---|
| DB 层 RSS | 12–14MB | > 30MB | 查是否有长事务/循环里借连接 |
| `-wal` 大小 | < 1MB | > 10MB 且持续 | §5.3 |
| `DBPoolTimeout` 日志 | 0 条/天 | > 5 条/小时 | 查事务里的慢 await、`--conns` 压测复现 |
| 单条 SQL 延迟 | < 5ms | > 50ms 持续 | `EXPLAIN QUERY PLAN` 看是否全表扫描 |
| 数据库文件 | < 20MB | > 48MB | 评估 VACUUM + 数据清理 |
| `integrity_check` | `ok` | 任何其它输出 | §5.4 |

> 2G 内存告急时，第一嫌疑永远是**图片渲染与缓存**（见 `docs/ARCHITECTURE.md` §4），
> 不要先怀疑数据库 —— 它的内存占用是有硬上限的（5 × 2MB cache）。

---

## 6. 给其他 Agent 的存储层约束（请务必遵守）

1. **不要自己写 SQL**，也不要在 service 里 import `aiosqlite`；所有读写走 `ctx.repo`。
2. **读-改-写**必须用 `mutate_bag` / `mutate_harem` / `consume_items` / `donate_materials`，
   不要 `get_*` 之后自己 `save_*`（并发下会丢更新，2G 机器上表现为"偶尔吞道具"）。
3. **事务里禁止 `await` 慢操作**：LLM（`ctx.llm.chat`）、渲染、网络、文件读写都不能在
   `db.transaction()` 内部调用；否则全局单写者被占住，整群排队，最终 `DBPoolTimeout`。
4. 金币只走 `add_coins` / `spend_coins`；发奖优先用 `context.grant()`（一次事务，避免半发）。
5. 不要在循环里逐条 `await repo.*`（>1000 次视为 bug）；需要批量时先聚合再一次性写。
6. 任何"每日"状态都加到 `settle_user_day` / `settle_harem_day` 里，**不要**新建定时器。