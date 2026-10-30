# 数据库设计（A1）· 赛博鲸鱼馆

> **为什么在 2G 机器上这样设计**（先看这 8 行，再看细节）：
> 1. 6 张表、3 条索引，所有查询都能走主键或等值前缀索引，没有全表扫描、没有 join；
> 2. 多值数据（背包/材料/贡献）用 **TEXT + JSON** 内联，一行只在一次事务里更新，
>    避免子表带来的页分裂与二级索引维护成本；
> 3. 连接池固定 **5** 条，每连接 `cache_size=-2000`（2MB 上限），DB 层稳态内存 ~12MB；
> 4. `WAL + synchronous=NORMAL + wal_autocheckpoint=200`：读不挡写，WAL 常驻 <1MB；
> 5. 写事务一律 `BEGIN IMMEDIATE`：把锁等待挪到事务开头，`busy_timeout` 才真正有效，
>    从根上消除"读锁升级写锁"的死锁；
> 6. 所有"每日状态"落成 `day_key` 文本字段，靠读时比较惰性重置 —— **没有定时任务**；
> 7. 时间戳有两种形态：业务比较用 INTEGER unix 秒，展示/排序用固定格式 TEXT（字典序=时间序）；
> 8. 结构唯一真源是 `core/db/schema.py::SCHEMA_SQL`，`sql/schema.sql` 是逐字副本 + 注释。

---

## 1. 表总览

| 表 | 粒度 | 主键 | 行数上界（单群 500 人） | 作用 |
|---|---|---|---|---|
| `users` | 一人一行 | `uid` | 500 | 鲸币、等级、签到/喂食/打工状态、惰性结算游标 |
| `bags` | 一人一行 | `uid` | 500 | 背包物品 + 鱼护（两个 JSON 字段） |
| `ponds` | 一群一行 | `group_id` | 1 | 池塘等级、材料库存、群友贡献度 |
| `harem` | (群, 目标) 一行 | `(group_id, target_uid)` | ≤ 500 | 婚姻/NTR/好感/保底计数 |
| `buffs` | (对象, 类型, 群) 一行 | `(subject_uid, kind, group_id)` | 峰值 ~1000 | 时效性增益（体验券、钟情卡护盾） |
| `kv` | 一键一行 | `k` | 两位数 | 杂项全局小状态 |

设计取舍：**没有外键、没有子表、没有触发器**。理由：
* 2 核机器上每一次多余的写都会放大 IO 与锁竞争；JSON 内联让"改背包"永远是一次单行 UPSERT；
* 无外键 = 删除用户时不会触发级联写风暴，删除逻辑显式写在 `Repo.delete_all_of_user`；
* 无触发器 = 行为全在 Python 里可见可测，不存在"数据库偷偷改了数据"的调试黑洞。

---

## 2. 三张核心表

### 2.1 `users`

```sql
CREATE TABLE users (
    uid TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    coins INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 1, exp INTEGER NOT NULL DEFAULT 0,
    checkin_streak INTEGER NOT NULL DEFAULT 0, last_checkin_day TEXT NOT NULL DEFAULT '',
    total_checkin INTEGER NOT NULL DEFAULT 0,
    feed_day TEXT NOT NULL DEFAULT '', feed_count INTEGER NOT NULL DEFAULT 0,
    feed_reward_day TEXT NOT NULL DEFAULT '',
    work_ready_ts INTEGER NOT NULL DEFAULT 0,
    total_earned INTEGER NOT NULL DEFAULT 0, total_spent INTEGER NOT NULL DEFAULT 0,
    day_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
);
```

| 字段 | 类型 | 含义 / 不变量 |
|---|---|---|
| `uid` | TEXT PK | 平台用户号。**所有玩法的主键**；用 TEXT 是因为 QQ 号可能超 2^31 且未来可能带平台前缀 |
| `name` | TEXT | 最近一次见到的昵称，只用于展示；每次交互可能被覆盖，**不可作为唯一标识** |
| `coins` | INTEGER | 鲸币余额。允许为负（透支由 service 自己保证不发生）；`NOT NULL DEFAULT 0` 保证 `add_coins` 的 `coins=coins+?` 不会遇到 NULL 变 NULL |
| `level` / `exp` | INTEGER | 等级 1..99，曲线 `100*level`；`add_exp` 循环升级，最多 98 次迭代（契约"循环 <1000"） |
| `checkin_streak` | INTEGER | 连续签到天数（游戏日），断签由 service 清零 |
| `last_checkin_day` | TEXT | 上次签到日 `YYYY-MM-DD`（游戏日，4 点分界），判连续/补签用 |
| `total_checkin` | INTEGER | 累计签到次数，只增不减（成就与统计口径） |
| `feed_day` | TEXT | **喂食计数所属日**。与 `day_key` 分开：喂食奖励与用户整体结算可以不同步 |
| `feed_count` | INTEGER | 当日喂食次数；超过 `FEED_DAILY_SOFT_LIMIT` 收益衰减 |
| `feed_reward_day` | TEXT | 当日喂食里程碑奖励的"已发"标记日（幂等：同日重复调用不发第二次） |
| `work_ready_ts` | INTEGER | 下次可打工的 unix 秒；`0` = 立即可打工。**用时间戳而不是剩余秒数**，停机不影响 |
| `total_earned` / `total_spent` | INTEGER | 累计收支，只增不减，用于对账（`coins` 变化必须与之匹配） |
| `day_key` | TEXT | 用户级惰性结算游标。`settle_user_day()` 发现与 `day_key()` 不等就地结算并返回 True |
| `created_at` / `updated_at` | TEXT | 固定格式 `YYYY-MM-DD HH:MM:SS`（见 §5 时间格式约定） |

写路径上的关键点：`coins` 的加减**只允许**走 `Repo.add_coins` / `Repo.spend_coins`
（SQL 里做 `coins=coins+?`，读-改-写会丢更新）；`spend_coins` 用
`WHERE uid=? AND coins>=?` 做 CAS，扣费失败返回 False 而不是抛异常。

### 2.2 `ponds`

```sql
CREATE TABLE ponds (
    group_id TEXT PRIMARY KEY, level INTEGER NOT NULL DEFAULT 1,
    materials TEXT NOT NULL DEFAULT '{}', contribution TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT ''
);
```

| 字段 | 含义 |
|---|---|
| `group_id` | 群号，一群一行；池塘是**群共享**资源，所以粒度必须到群 |
| `level` | 1..`POND_MAX_LEVEL(10)`；`Pond.from_row` 会 `max/min` 夹紧，坏数据不会把加成算爆 |
| `materials` | JSON `{material_id: 存量}`，见 §3.1 |
| `contribution` | JSON `{uid: 累计捐献材料数}`，见 §3.2 |
| `updated_at` | 最后写入时间（运维排查用） |

升级语义在 `Repo.take_pond_upgrade` 里：一个事务内读池塘 → 比对
`POND_UPGRADE_REQUIREMENTS[level+1]` → 够则扣材料并 `level+1` → UPSERT。
返回 `(pond, 是否升级, 本次消耗)`，不升级时不写库（事务空提交，代价可忽略）。

### 2.3 `harem`

```sql
CREATE TABLE harem (
    group_id TEXT NOT NULL, target_uid TEXT NOT NULL,
    target_name TEXT NOT NULL DEFAULT '', owner_uid TEXT NOT NULL DEFAULT '',
    owner_name TEXT NOT NULL DEFAULT '', affection INTEGER NOT NULL DEFAULT 0,
    marry_fail INTEGER NOT NULL DEFAULT 0, ntr_fail INTEGER NOT NULL DEFAULT 0,
    divorce_fail INTEGER NOT NULL DEFAULT 0, ntr_count INTEGER NOT NULL DEFAULT 0,
    chase_count INTEGER NOT NULL DEFAULT 0, day_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, target_uid)
);
```

| 字段 | 含义 / 不变量 |
|---|---|
| `(group_id, target_uid)` | 复合主键 = "这个群里这个人的关系记录"，天然幂等：`INSERT OR IGNORE` 即可创建 |
| `target_name` / `owner_name` | 冗余昵称。**故意不 join users**：榜单一次查 10 行就能出图，省一次查询与一次内存拼装 |
| `owner_uid` | 拥有者；**空串 = 公共老婆**（可攻略）。用 `''` 而不是 NULL，避免三值逻辑让 `<>''`/`=''` 的语义变复杂 |
| `affection` | 好感度；跨天由 `settle_harem_day` 按天数衰减（`PURSUIT_DECAY_PER_DAY`） |
| `marry_fail` / `ntr_fail` / `divorce_fail` | **当日**失败计数，用于保底（第 5/2/10 次必中）；跨天清零 |
| `ntr_count` | 累计被 NTR 成功次数，只增；绿帽榜排序键 |
| `chase_count` | 累计被追求次数，只增；公共老婆榜排序键 |
| `day_key` | 该行的结算游标（惰性清零保底计数 + 衰减好感） |
| `created_at` | 关系创建时间；`list_harem` 用 `ORDER BY created_at DESC` 表示"最近娶的在前"（依赖固定时间格式，见 §5） |

一个 (群,目标) 只能有一条记录 → 两个群友同时抢同一个人，靠
`mutate_harem` 的 `BEGIN IMMEDIATE` 串行化，后来的那位会看到前者已经写入的 `owner_uid`，
业务上直接判 "已经有主了"，不会出现双写覆盖。

---

## 3. JSON 字段结构

### 3.1 `ponds.materials`

```json
{"mat_stone": 12, "mat_coral": 3, "mat_bone": 24, "mat_crystal": 8}
```

* 键：`data/items.py` 里 `category == "mat"` 的 `ItemDef.id`（`mat_*`）；
* 值：正整数。扣到 0 时 `take_pond_upgrade` 会 `pop` 掉键，**不保留 0 值**（保持 JSON 最小）；
* 解析统一走 `models.parse_int_dict`：非数字/非正数被静默丢弃，坏数据不会让升级逻辑抛异常。

### 3.2 `ponds.contribution`

```json
{"123456789": 20, "987654321": 5}
```

* 键：群友 `uid`（TEXT，**不是昵称**——昵称会变，uid 不会）；
* 值：该群友累计投入的材料个数，只增不减（不随升级扣除，用于"贡献榜/感谢名单"）；
* 行数上界 = 群活跃人数（500），JSON 大小 ~10KB 封顶，仍是"一次单行写"。

### 3.3 `bags.items`

```json
{"rice_bowl": 3, "bait_normal": 12, "card_wife": 1}
```

* 键：`ItemDef.id`；值：1..`ITEM_STACK_LIMIT(9999)`，超上限按 9999 截断，扣到 0 删键；
* `Bag.add/remove` 是唯一修改入口（保证不出现 0/负数）；外部传入的字典先过
  `Repo._clean_items`（丢弃 None / 非数字 / 非正数），脏输入不会把事务打回。

### 3.4 `bags.fish`

```json
[{"sp": "li_yu", "g": 1800, "p": 65, "ts": 1761300000}]
```

| 键 | 含义 | 约束 |
|---|---|---|
| `sp` | 鱼种 id（`data/fish.py::FishDef.id`） | **必填非空**，`models.parse_bag` 用它过滤脏记录；旧 id 找不到时由展示层兜底 |
| `g` | 重量（克） | `randint(min_g, max_g)` |
| `p` | 入护时的估值鲸币 | 卖鱼/溢出折算都直接用这个数，避免"入库后价格被改"导致对不上账 |
| `ts` | 入护 unix 秒 | 排序/展示用 |

* 键名刻意用缩写（`sp/g/p/ts`）：80 条鱼约 7KB，若用 `species_id/weight_grams/price/timestamp`
  会翻倍 —— 这是 bags 表体积最大的一项，值得抠。
* 长度上界 = `cfg.fish_bag_limit`（默认 80，硬上限 200）；`Bag.add_fish` 在内存里裁掉最旧的，
  被挤出的鱼按 `p` 折算鲸币 —— **这是唯一能压住 bags 体积的机制**，
  没有它鱼护会无限增长（一人 30 天能钓上千条）。
* 存 JSON 数组而不是一行一条：读背包一次 IO 拿到全部，卖鱼/展示不需要 N 次查询。

### 3.5 `buffs.payload`

```json
{"mult": 2, "level": 1}
```

* **只支持 int 值**：`Repo.get_buff` 用 `models.parse_int_dict` 解析，字符串/浮点会被丢弃；
  需要复杂结构请改存 `kv`（约定 `"json:"` 前缀）；
  现状：`services/items.py` 会写入 `{"src": item.id, "by": uid}`（字符串），
  读回来时这两个键会消失 —— 目前全仓没有任何代码依赖它们（只判 `is not None`），
  属于"可写不可读"的冗余字段；将来要用请先改 `get_buff` 的解析方式（见 STORAGE_AUDIT K-9）；
* `get_buff` 返回时会把 `expires_ts` 合并进 payload（方便 service 算剩余时间），
  写入时不要自带 `expires_ts`。

### 3.6 `kv`

* `k` 短键（建议 `域:名`，如 `notice:version`）；`v` 任意 TEXT，结构化值约定 `json:` 前缀；
* 行数应保持两位数：它是"小状态"表，不是缓存表，禁止往里塞列表数据。

---

## 4. 索引设计

现有 3 条（`idx_harem_owner`、`idx_harem_affection`、`idx_buffs_expire`）+ 各表主键。

| 索引 | 支撑的查询 | 为什么这么建 |
|---|---|---|
| `harem` PK `(group_id,target_uid)` | `get_harem` / `mutate_harem` / `ensure_harem` / `save_harem` | 等值双列，点查；同时天然保证"一群一人一条"的唯一性，不用额外唯一索引 |
| `idx_harem_owner (group_id, owner_uid)` | `list_harem`、`harem_count`、`group_overview` | 两列全等值 → 直接定位到"某人在本群的后宫"这一小段索引，行数 ≤ 几十 |
| `idx_harem_affection (group_id, affection DESC)` | `affection_ranking`：`WHERE group_id=? ORDER BY affection DESC, created_at ASC LIMIT 10` | 前缀等值 + 第二列已按需求方向排序，SQLite 直接顺着索引取 10 行，**不需要临时 B 树排序**（2 核机器上排序是纯 CPU 浪费） |
| `idx_buffs_expire (expires_ts)` | 启动时 `DELETE FROM buffs WHERE expires_ts<=?`、`active_buffs` 的时间过滤 | 范围扫描，避免全表；buffs 行数少但清理动作每次启动都跑 |
| `users`/`bags`/`ponds`/`buffs` PK | 全部点查 | 这些表 90% 的访问是"按 uid/群号取一行"，主键即最优 |

**为什么不多建索引**：
* 每个二级索引都会让写事务多改一棵 B 树；2 核机器上写放大直接体现为延迟；
* 榜单查询都有 `LIMIT 10`，即使退化成"扫本群切片"也只有 ≤500 行，仍远小于一次 PNG 渲染的成本。

**已知的索引缺口（建议，需队长批准后改 `schema.py` + `sql/schema.sql`）**：

```sql
-- public_targets: WHERE group_id=? AND owner_uid='' ORDER BY chase_count DESC LIMIT 10
CREATE INDEX IF NOT EXISTS idx_harem_chase  ON harem(group_id, chase_count DESC);
-- ntr_ranking:   WHERE group_id=? AND ntr_count>0 ORDER BY ntr_count DESC LIMIT 10
CREATE INDEX IF NOT EXISTS idx_harem_ntr    ON harem(group_id, ntr_count DESC);
```

当前不加也能跑（每次多排 ≤500 行，单次 <1ms），属于"数据量再涨 10 倍时再上"的优化。

---

## 5. 时间格式约定（很容易踩）

| 形态 | 例子 | 用在哪 | 规则 |
|---|---|---|---|
| 游戏日 | `2026-10-24` | `day_key` / `last_checkin_day` / `feed_day` | 由 `time_utils.day_key()` 生成，**4 点前算前一天**；跨天比较只用字符串相等或 `days_between` |
| 展示时间 | `2026-10-24 21:30:05` | `created_at` / `updated_at` | 固定 `%Y-%m-%d %H:%M:%S`，**字典序 = 时间序**，所以可以直接 `ORDER BY created_at DESC`；禁止写 ISO 的 `T` 分隔或带时区后缀，否则排序会错 |
| unix 秒 | `1761300000` | `work_ready_ts` / `buffs.expires_ts` | 只做数值比较，停机/改时区不影响；比较时统一用 `time_utils.now_ts()` |

绝对不要在 DB 里存 `datetime` 对象或本地化字符串混用 —— 本插件只用上表三种形态。

---

## 6. WAL 读写并发 与 `BEGIN IMMEDIATE` 防死锁

### 6.1 WAL 下谁挡谁

```
                 写者（同一时刻只能有 1 个）
                       │
   ┌───────────────────┼────────────────────┐
   ▼                   ▼                    ▼
 读连接 A            读连接 B            读连接 C      ← 各读各的快照，互不阻塞
 （快照 S1）        （快照 S2）         （最新）
```

* WAL 把新页写进 `-wal` 文件，读连接继续读自己的快照 → **读不阻塞写，写不阻塞读**；
* 但"写者"仍然**全局唯一**：第二个写事务必须排队；
* 排队的正确姿势是 `busy_timeout`（我们设 5000ms），让 SQLite 自己等，而不是立刻报错。

### 6.2 为什么一定会用 `BEGIN IMMEDIATE`

SQLite 的锁是"升级式"的：一个事务可以先拿 SHARED（读），再尝试升级成 RESERVED（写）。
**升级失败时 SQLite 不能等**：它在等对方释放 SHARED，而对方也在等它 —— 这就是死锁，
所以 SQLite 直接返回 `SQLITE_BUSY`，`busy_timeout` 对升级路径基本无效。
在 WAL 里还有一种更糟的 `SQLITE_BUSY_SNAPSHOT`：快照过期，等多久都没用，只能回滚重来。

`BEGIN IMMEDIATE` 在**事务第一条语句之前**就申请 RESERVED 锁：

```python
async with db.transaction() as conn:      # ← 这里就 BEGIN IMMEDIATE
    row = await conn.execute("SELECT ...")   # 读
    await conn.execute("UPDATE ...")         # 写
# COMMIT
```

* 竞争只发生在 `BEGIN` 这一瞬间，这正是 `busy_timeout` 能生效的场景（等 5s，等不到才报错）；
* 不会有"读着读着要写"的升级动作，也就不存在升级死锁；
* 对 JSON 字段尤其重要：`SELECT items → 改 → UPDATE items` 必须在**同一个 IMMEDIATE 事务**里，
  否则两个请求会读到同一份旧背包，后写的覆盖先写的（丢物品）。

这就是 `mutate_bag` / `mutate_harem` / `donate_materials` / `take_pond_upgrade` / `spend_coins`
统一用 `db.transaction()` 的原因。

**三条铁律（其他 Agent 必须遵守）**：
1. 读-改-写必须用 `mutate_bag` / `mutate_harem`，不要 `get_bag()` 之后自己 `save_bag()`；
2. **事务里禁止 `await` 任何慢操作**（LLM、Pillow、网络、文件）：会把写锁握住好几秒，
   其他群友的写全部排队 → 表现为"机器人卡住"；
3. 一个 Repo 方法只借一次连接；不要在 `transaction()` 内部调用另一个 Repo 方法（重入即死锁）。

### 6.3 连接池 5 条的并发上限

* 5 条连接 = 最多 5 个并发 SQL，第 6 个在 `asyncio.Queue` 上排队，超过 6s 抛 `DBPoolTimeout`
  （转成"数据库繁忙，请稍后再试"的提示卡，而不是让用户等在那里没反应）；
* 写事务天然串行，所以真正并发的只有读；单条 SQL 都在毫秒级，
  5 条连接足以扛住"500 人群 + 防抖后每秒个位数请求"；
* 为什么不设更大：SQLite 写者唯一，连接多了只是让更多协程在锁上排队，**反而增加内存与上下文切换**。

---

## 7. 内存占用估算（连接池 5 × `cache_size=-2000`）

| 项 | 单条连接 | 5 条合计 | 说明 |
|---|---|---|---|
| SQLite page cache | ≤ 2000 KB | **≤ 10 MB** | `cache_size=-2000` 的单位是 KiB（负数），即每连接 2MB 上限；这是**上限**，按需增长，实际通常只缓存到库文件大小 |
| WAL / shm | -shm 32 KB，WAL ≤ ~1 MB（见 §8） | ~1 MB | shm 是固定 32KB 的索引；WAL 由 `wal_autocheckpoint=200` 压住 |
| aiosqlite 连接 + 线程 | ~几十 KB RSS（线程栈是虚拟保留） | ~0.3–0.5 MB | 每条连接 1 个 Python 线程 + 1 个 sqlite3 handle |
| 语句/临时结构 | 几 KB | 几 KB | `temp_store=MEMORY`，但临时表都很小 |
| **合计** | | **稳态 12–14 MB，硬上限 ~15 MB** | 相对 2G 总内存可接受；NapCat + AstrBot 才是大头 |

补充说明：
* SQLite 编译默认 `cache_size` 恰好也是 -2000，我们显式写出来，是为了防止被 AstrBot 全局 PRAGMA
  或未来版本默认值变化悄悄改掉；
* `mmap_size=0`：关掉 mmap 是为了让 RSS 反映真实占用，避免"看起来占了很多"被 OOM Killer 优先杀；
* 库文件只有几 MB 时，5 条连接各自缓存的页高度重叠，实际 RSS 通常明显低于 10MB 上限。

---

## 8. WAL 增长与 checkpoint 策略

WAL 文件只增不减的经典原因：**有读事务长期持有旧快照**，checkpoint 无法回收它之后的帧。
本插件的设计从根上排除了这种情况：

* 所有读都是"单语句 + 立刻关游标"（`fetch_one/fetch_all` 内部 `finally: cursor.close()`），
  不存在跨 `await` 的长读事务；
* 写事务内禁止慢 await（见 §6.2 铁律 2），事务存活时间在毫秒级。

三层控制：

| 机制 | 设置 | 效果 |
|---|---|---|
| 自动 checkpoint | `PRAGMA wal_autocheckpoint=200`（约 200×4KB ≈ 800KB） | 写者每次 COMMIT 时机会式把帧回写主库，WAL 稳态 < 1MB |
| 关闭时截断 | `Database.close()` 里 `PRAGMA wal_checkpoint(TRUNCATE)` | 正常停机把 WAL 截到 0 字节，不留尾巴 |
| 手工兜底 | `PRAGMA wal_checkpoint(TRUNCATE)` / `(PASSIVE)` | 见 `docs/STORAGE_AUDIT.md` 运维 checklist；`TRUNCATE` 需要没有活跃读事务 |

注意：`-wal` / `-shm` 是数据库的一部分，**备份时必须连它们一起拷，或在停机后拷贝**（见运维 checklist）。
WAL 上限估算：稳态 ≤ 1MB；如果某次批量写（例如一次 `delete_all_of_user` 清了 500 行）超过 200 页，
WAL 会临时涨到几 MB，checkpoint 后回落 —— 属于正常现象，不需要处理。

---

## 9. VACUUM 触发条件

* 代码入口：`Database.vacuum_if_needed(threshold_mb=48.0)`，只看**主库文件大小**；
  `< 48MB` 直接返回 False（绝大多数群永远到不了这个阈值）。
* 为什么必须"有条件"：VACUUM 会重写整个库文件 —— 2 核机器上这是几百毫秒到数秒的纯 IO，
  期间拿写锁，还会临时占用约 **2 倍库大小** 的磁盘（40G 够，但会和 NapCat 抢 IO）。
* 触发时机建议（任选其一，**不要每天跑**）：
  1. 插件启动、且 `db 文件 > 48MB`；
  2. 大批量删除之后（`delete_all_of_user`、prune 鱼护）且当前无人在线；
  3. 运维窗口手工执行 `sqlite3 whale.db "VACUUM;"`。
* 判断"该不该 VACUUM"更准的指标是空闲页比例：

```sql
PRAGMA page_count;      -- 总页数
PRAGMA freelist_count;  -- 空闲页数；freelist/page_count > 0.25 才值得 VACUUM
```

* 更便宜的替代：`PRAGMA optimize;`（每次启动跑一次，毫秒级，只更新统计信息，让查询计划更准）。
* 注意：库开了 WAL，VACUUM 不能在有活跃事务/读快照时执行；失败会抛 `database is locked`，
  `vacuum_if_needed` 已把它吞成 `False` + 一条 warning，不影响主流程。

---

## 10. 数据量估算（500 人群 × 30 天）

前提：单群 500 人，日活 100，30 天；每行记录头 + 列值按 SQLite 实测经验值估算。

| 表 | 行数 | 单行大小 | 30 天总量 | 备注 |
|---|---|---|---|---|
| `users` | 500 | ~200 B | **~0.1 MB** | 只有交互过的人才建行；字段全定长整数 + 短文本 |
| `bags` | 500 | 典型 ~1.5 KB（items ~250B + 8 条鱼 ~0.7KB）；最坏 ~8 KB（80 条鱼） | **典型 0.8 MB / 最坏 4 MB** | 唯一会持续长的表；被 `fish_bag_limit=80` 硬性封顶 |
| `ponds` | 1 | ~500 B（materials + contribution JSON） | **< 1 MB** | contribution 最多 500 个键 ≈ 10KB |
| `harem` | ≤ 500 | ~140 B | **~0.07 MB** | 粒度是 (群,目标)，500 人最多 500 行 |
| `harem` 2 条索引 | 500 | ~25 B/条 | ~0.03 MB | 索引页 |
| `buffs` | 峰值 ~200（体验券+护盾） | ~150 B | **< 0.1 MB** | 过期即删，不累积 |
| `kv` | < 20 | < 100 B | 可忽略 | |
| WAL | — | — | **≤ 1 MB** 稳态 | §8 |
| **合计（单群）** | | | **典型 ≈ 1.5 MB，最坏 ≈ 5 MB** | |
| **合计（3 个 500 人群）** | | | **典型 ≈ 5 MB，最坏 ≈ 15 MB** | 加上页碎片余量按 ×1.2 ≈ 18 MB |

结论：
* 40G 磁盘对这个量级有 **3 个数量级**的余量，磁盘不是约束；
* 真正的约束是内存与写锁：整库 < 20MB，单个连接的 2MB page cache 就能覆盖大部分热页，
  查询基本是"内存命中 + 主键定位"，不会触发随机 IO；
* 增长是**次线性**的：`fish` 列表被容量上限裁掉、物品是整数计数、harem 行数受群人数封顶、
  buffs 自动过期 —— 30 天后不会出现"越跑越慢"；
* 只有一种情况会爆：`fish_bag_limit` 被配置成 200 且 500 人全部装满 → 500 × 200 × 100B ≈ 10MB/群，
  仍在可接受范围，但渲染端要注意别一次把整个鱼护塞进 Card（`Card.validate` 会裁到 8 行）。

---

## 11. 写路径速查（给其他 Agent）

| 需求 | 正确写法 | 错误写法 |
|---|---|---|
| 加/减鲸币 | `await repo.add_coins(uid, delta)` / `spend_coins(uid, cost)` | 自己读 `user.coins` 再 `update_user(coins=...)`（丢更新） |
| 加物品 | `await repo.add_items(uid, {"rice_bowl": 1})` | `bag = get_bag(); bag.add(...); save_bag()`（并发丢物品） |
| 扣物品 | `ok, bag = await repo.consume_items(uid, {...})` | 先 `count()` 判断再 `mutate_bag`（中间可能被别的请求抢走） |
| 改关系 | `await repo.mutate_harem(gid, target, fn, name)` | `get_harem` + 改 + `save_harem`（双写覆盖） |
| 池塘捐献 | `await repo.donate_materials(gid, uid, {...})` | 自己拼 materials/contribution 再 `save_pond` |
| 发奖（币+物+经验） | `await grant(ctx, uid, coins=..., items=..., exp=...)` | 三次独立调用（多两次事务；中途失败会半发） |
| 每日重置 | `settle_user_day(user)` / `settle_harem_day(row)` | 任何形式的定时器/批更新 |