# 赛博鲸鱼馆 · 架构说明（A1 存储与架构）

> 目标机：2 核 2G 40G，与 NapCat + AstrBot 同机。
> 一句话架构：**指令层只管路由与兜底，service 只算数值，repo 独占 SQL，renderer 独占 Pillow**。
> 四条硬边界：service 不碰 PIL、service 不写 SQL、repo 不 import service、任何地方都没有定时器。

---

## 1. 项目结构树（文件级 + 负责人）

```
D:\Plugin_Astr\
├─ main.py                    队长     插件入口：指令注册、装配、防抖、渲染、异常兜底
├─ metadata.yaml              队长     插件元信息（name=astrbot_plugin_whale_harem）
├─ _conf_schema.json          队长     AstrBot WebUI 配置项 schema
├─ requirements.txt           队长     aiosqlite / Pillow，就这两个
│
├─ sql\
│  └─ schema.sql              A1       可独立执行的建表脚本（SCHEMA_SQL 的可读副本 + PRAGMA + JSON 注释）
├─ scripts\
│  ├─ bench_db.py             A1       纯 asyncio 压测（200 并发 × 4 类 DB 操作，P50/P95/max + 泄漏检测）
│  ├─ smoke_test.py           队长     离线冒烟：不起 AstrBot，跑通全部 service 链路（36 断言）
│  ├─ integration_stub_test.py 队长    内置 astrbot stub，真实 main.py 走 21 条指令（26 断言）
│  ├─ test_edge_cases.py      队长     缓存/跨天重置/保底/顶层加载（19 断言）
│  ├─ render_demo.py          队长     8 张典型卡片出图，肉眼验收排版与美术
│  └─ sim_economy.py          队长     20 万竿蒙特卡洛，核对 BALANCE 期望收益/掉率
│
├─ core\                      队长（A1 只对 database.py / decorators.py 做兼容性加固）
│  ├─ __init__.py             队长
│  ├─ constants.py            队长     所有数值/颜色/概率；只允许追加，不许改已有名字
│  ├─ config.py               队长     PluginConfig（带边界夹紧 + 安全默认值）
│  ├─ context.py              队长     SvcCtx / settle_user_day / settle_harem_day / grant
│  ├─ decorators.py           A1       debounce / require_group / safe_handler
│  ├─ errors.py               队长     WhaleError 家族（message 直接给用户看）
│  ├─ log.py                  队长     优先用 AstrBot logger，脱框架退化 logging
│  ├─ models.py               队长     User / Pond / HaremRow / Bag + JSON 防御式解析
│  ├─ paths.py                队长     plugin_data 目录解析（插件目录只读）
│  ├─ rng.py                  队长     O(1) Alias 采样器（启动建表，运行期零分配）
│  ├─ time_utils.py           队长     day_key()：凌晨 4 点分界，惰性重置的唯一开关
│  ├─ async_utils.py          队长     Debouncer / KeyedLock / LimitedRunner / to_thread / collect_now
│  └─ db\
│     ├─ __init__.py           队长     门面导出
│     ├─ schema.py             队长     SCHEMA_SQL（唯一真源）+ SCHEMA_VERSION=1
│     ├─ database.py           队长+A1   固定 5 连接池、WAL、BEGIN IMMEDIATE、VACUUM/统计
│     └─ repos.py              队长+A1   唯一 SQL 出口：users/bags/ponds/harem/buffs/kv
│
├─ data\                      物品与掉落：数据的唯一真源
│  ├─ items.py                队长     ItemDef 注册表（id/名/品质/类别/价格/别名）
│  ├─ fish.py                 A3       鱼种表 + 单条鱼获记录结构
│  └─ drops.py                A3       掉落权重池、品质档位、体验券倍率
│
├─ services\                  纯计算层：入参 (SvcCtx, uid, ...)，出参 ui.schema.Card
│  ├─ daily.py                A2       签到 / 补签
│  ├─ feed.py                 A2       喂食（好感、里程碑、LLM 台词）
│  ├─ work.py                 A2       打工（冷却、随机收益）
│  ├─ fishing.py              A3       钓鱼 / 鱼护 / 卖鱼
│  ├─ pond.py                 A3       池塘面板 / 捐献 / 用升塘卡
│  ├─ marriage.py             A4       娶 / 离 / NTR / 后宫 / 榜单
│  ├─ shop.py                 A4       商店 / 购买
│  ├─ items.py                A4       使用道具（分发到各玩法）
│  └─ profile.py              队长     个人主页 / 帮助
│
├─ ui\                        渲染层：只吃 Card 数据，只吐图片
│  ├─ schema.py               队长     Card / Block / Stat / Row 协议（只读，不许改）
│  ├─ theme.py                A5       配色、字体、圆角、间距（DeepSeek 蓝+赛博鲸鱼）
│  ├─ fatfish.py              A5       肥鱼/池塘/插画绘制（纯 Pillow 图元）
│  ├─ cache.py                A5       图片 LRU + TTL（默认 24 张 / 300s）
│  └─ renderer.py             A5       Card -> PNG 文件路径（线程池内执行，渲染闸门内）
│
└─ docs\
   ├─ CONTRACT.md             队长     开发契约（最高优先级）
   ├─ ARCHITECTURE.md         A1       本文：结构树 / 依赖图 / 钓鱼调用链 / 并发模型
   ├─ DB_DESIGN.md            A1       表设计、JSON 结构、WAL/锁/连接池/容量估算
   ├─ STORAGE_AUDIT.md        A1       存储层审查结论 + 2G 机器运维 checklist
   ├─ ANTI_CRASH.md           队长     2核2G 防崩溃避坑指南
   ├─ REVIEW_REPORT.md        A5/队长  对抗式审查：P0/P1/P2、修复位置、上线前确认
   ├─ REVIEW_ROUND2.md        A5       第二轮复核：渲染层 API + 数字独立复算
   ├─ BALANCE.md              A5/队长  数值平衡与经济闭环表（掉率/期望/旋钮）
   ├─ TEST_REPORT.md          队长     真机 4 套测试 + 性能数据 + 出图预览
   └─ DELIVERY.md             队长     交接单：怎么装、怎么验、已知取舍
```

> 说明：`services/` 的文件到 Agent 的映射按契约第 2 节的分工推定（A2=养成经济、A3=钓鱼池塘、A4=社交商店），
> 队长若在 `main.py` 装配时调整归属，以队长登记为准——**目录结构与依赖方向不受影响**。

---

## 2. 模块依赖图

实线 = 允许的依赖方向；`✗` = 明令禁止（违反即返工）。

```
                       ┌───────────────────────────┐
                       │        main.py (队长)      │
                       │  指令路由 / 防抖 / 兜底     │
                       └───┬───────┬───────────┬───┘
             ┌─────────────┘       │           └──────────────┐
             ▼                     ▼                          ▼
   ┌──────────────────┐   ┌──────────────────┐      ┌──────────────────┐
   │ core.decorators  │   │   services.*     │      │   ui.renderer    │
   │ debounce/guard   │   │  （纯计算，Card） │      │  Card -> PNG     │
   └───────┬──────────┘   └───┬──────┬───────┘      └────┬───────┬─────┘
           │                  │      │                   │       │
           ▼                  ▼      ▼                   ▼       ▼
   ┌──────────────┐   ┌────────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐
   │ async_utils  │   │ core.ctx   │ │ ui.schema│ │ ui.theme │ │ui.cache│
   │ Debouncer    │   │ SvcCtx     │ │ (纯数据) │ │ui.fatfish│ │ LRU+TTL│
   └──────┬───────┘   └─────┬──────┘ └──────────┘ └────┬─────┘ └────────┘
          │                 │                          │
          ▼                 ▼                          ▼
   ┌──────────────┐   ┌──────────────┐            Pillow (to_thread)
   │ time_utils   │   │  db.repos    │
   │ day_key      │   │  唯一 SQL 出口│
   └──────┬───────┘   └──────┬───────┘
          │                  ▼
          │           ┌──────────────┐
          │           │ db.database  │ 5 连接 / WAL / IMMEDIATE
          │           └──────┬───────┘
          │                  ▼
          │              aiosqlite ──► whale.db(-wal/-shm)
          ▼
   ┌──────────────┐
   │ constants.py │◄──── data.items / data.fish / data.drops（只读常量）
   └──────────────┘

禁止的边：
  services.*  ──✗──►  PIL / ui.renderer / ui.theme / ui.fatfish
  services.*  ──✗──►  aiosqlite / 裸 SQL（必须经 ctx.repo）
  core.db.*   ──✗──►  services.* / main.py（单向依赖，不能反向）
  ui.*        ──✗──►  core.db.*（渲染层不查库，数据由 service 拼好塞 Card）
  anyone      ──✗──►  asyncio.create_task(每日循环) / APScheduler / sleep 轮询
```

数据流一句话：`event → main → service → (SvcCtx → Repo → SQL) → Card → renderer → Pillow → PNG 路径`。

---

## 3. 一次「钓鱼」请求的完整调用链

指令：`/钓鱼` 或 `/钓鱼 蚯蚓`（A3 的 `services/fishing.py::do_fish`）。

```
① 平台事件
   NapCat ──► AstrBot ──► main.py::on_command("/钓鱼")
   main 抽出 uid / 昵称 / group_id / umo，做群白名单判断

② 第一层刹车：防抖 + 串行（main.py::_run 内，不用装饰器）
   1. 群白名单判断（cfg.enabled_groups）
   2. self._debouncer.allow(uid)        ← 同一 uid 1.5s 内只放行一次
      └─ 命中 → 直接 return None（不发任何消息，零 IO，连错误卡都不回）
   3. async with self._keyed.hold(uid)  ← 同 uid 串行，保证"读-改-写"不丢计数
   4. asyncio.wait_for(handler(...), 20) ← 业务级硬超时，防止某个 service 卡死拖垮队列
   （core/decorators.py 另外提供了等价的 @debounce/@require_group/@safe_handler 装饰器，
     供其他插件或后续重构复用；本插件走 _run 是因为防抖窗口来自运行期配置。）

③ 指令层 → service
   card = await do_fish(ctx, uid, name, group_id, bait_id="bait_worm", umo)
   （ctx = SvcCtx(repo, cfg, llm, plugin_name)）

④ service 读用户 + 惰性日结（唯一的"每日重置"时刻）
   user = await ctx.repo.get_user(uid, name)        -- SELECT users（PK 命中）
   if settle_user_day(user):                        -- day_key 不一致就地结算
       await ctx.repo.update_user(uid, day_key=..., feed_day=..., feed_count=...)
   pond = await ctx.repo.get_pond(group_id)          -- SELECT ponds（PK 命中）

⑤ 计算上钩率 + 扣饵（空军也扣饵，这是经济闭环的关键）
   bag  = await ctx.repo.get_bag(uid)                -- SELECT bags（PK 命中）
   hook_rate = clamp(HOOK_BASE_RATE + pond_bonus + bait_bonus + ticket_bonus,
                     HOOK_RATE_FLOOR, HOOK_RATE_CEIL)
   buff = await ctx.repo.get_buff(uid, "ticket_private", group_id)   -- 命中索引
   ok, bag = await ctx.repo.consume_items(uid, {bait_key: 1})
   -- BEGIN IMMEDIATE; SELECT bags(uid); 校验数量; UPSERT bags; COMMIT
   if not ok: raise NotEnoughItem(...)               → 错误卡
   if not chance(hook_rate): → 空军卡（饵已消耗，不再改任何数据，也不抛异常）

⑥ 掷骰 + 一次事务写背包，随后原子加币/加经验
   category = CATEGORY_SAMPLER.pick()          -- fish/prop/item 三选一，O(1) Alias
   fish_def = fish.random_fish(star)           -- star 由品质采样器 + 池子加成得出
   grams = randint(fish_def.min_g, fish_def.max_g); price = fish.sell_value(fish_def, grams)
   mat_id = material_sampler(pond.level).pick(); mat_num = randint(1, 2)   -- 上钩必掉材料
   record = {"sp": fish_def.id, "g": grams, "p": price, "ts": now_ts()}   -- 短键，省 DB 空间
   state = {"overflow_coins": 0}

   def _mut(bag: Bag) -> None:                       -- 同步闭包，事务内执行，禁止 await
       state["overflow_coins"] = sum(r.get("p", 0) for r in bag.add_fish(record, limit))
       bag.add(mat_id, mat_num)                      -- 必掉材料
       bag.add(item_id, 1)                           -- 道具/物品（若有）

   bag = await ctx.repo.mutate_bag(uid, _mut)
   -- BEGIN IMMEDIATE; SELECT bags; 改内存 Bag; UPSERT bags; COMMIT（一次只写一行）
   balance = await ctx.repo.add_coins(uid, coin_win + state["overflow_coins"])
   level, _exp, leveled = await ctx.repo.add_exp(uid, FISH_EXP)   -- 各自独立事务

⑦ service 组装 Card（纯数据，不碰 PIL、不碰文件）
   c = card(f"上鱼了！{fish_def.name}", art="fish:4", accent=fish_def.color,
            stats=[stat("鲸币", balance), stat("等级", f"Lv.{level}")], tone="success")
   c.blocks.append(kv_block("本次收获", [kv("鱼获", fish_def.name), kv("重量", f"{grams} g"),
                                        kv("估价", f"{price} 鲸币")]))
   return c.validate()                               -- 裁剪超长字段，防渲染层被打爆

⑧ 第二层刹车：渲染闸门 + 缓存（main.py::_emit 统一渲染）
   path = await self._renderer.render(card)           -- ui.renderer.Renderer
     ├─ cache.get(card.cache_key())  → 命中直接回本地 PNG 路径（省 CPU/省内存）
     ├─ LimitedRunner(render_concurrency=1~2).slot()   ← 同时最多 1~2 张图在画
     └─ await to_thread(self._render_sync, card)       ← Pillow 在工作线程，事件循环不阻塞
        └─ 画完立刻 del Image / del Draw，再 collect_now(min_interval=2s)
   渲染异常 → RenderError → main.py 降级为 card.to_text() 纯文本，保证用户一定有回复

⑨ 回消息
   return event.image_result(path)  （渲染失败则 event.plain_result(card.to_text())）

全程 IO：3~5 条走主键/索引的 SQL（毫秒级）+ 1 次 PNG 渲染（几十毫秒，线程池内）。
没有一次 sleep、没有一个后台任务、没有一次全表扫描。
```

异常路径（同样重要。下表以 `core/decorators.py` 装饰器链路为准，`main.py::_run` 是等价实现）：

| 环节 | 异常 | 结果 |
|---|---|---|
| ② 防抖 | `DebouncedError` | main 静默丢弃，无回复、无日志噪音 |
| ② 私聊 | `NotInGroup` | safe_handler → 错误卡文本 |
| ④/⑤/⑥ 缺道具、余额不足 | `NotEnoughItem` / `NotEnoughCoins` | safe_handler → 错误卡文本 |
| ⑥ 锁等待超时 / 磁盘抖动 | `DBPoolTimeout`（database.py 转译） | 错误卡"数据库繁忙，请稍后再试" |
| ⑦ 任何未预期异常 | `Exception` | safe_handler 记「函数名+类型+截断消息」后返回 None，绝不冒泡到 AstrBot |

---

## 4. 并发模型：三层刹车

2 核 2G 上真正的敌人不是"算得慢"，而是**同一时刻太多协程同时要内存、同时要写锁、同时要画图**。
所以从外到内设三道闸，越靠外越便宜：

### 第一层：单用户防抖（零 IO，内存 ~几百字节/人）

* 两种等价实现，二选一即可：
  * `main.py::_run`：`self._debouncer.allow(uid)`（窗口来自运行期配置 `cfg.command_debounce_ms`）—— 当前线上路径；
  * `core/decorators.py::debounce`：装在指令入口，状态 `self._debouncer_<fn>`（每实例每指令一份）。
* 状态只是 `{uid: 上次通过时间}`（`core.async_utils.Debouncer`），超过 512 条惰性 prune；
  插件热重载 → 实例回收 → 状态自动消失，不会残留旧冷却。
* 作用：群友连点/复读时，99% 的请求在第一条 `await` 之前就被丢掉，DB 连接、渲染闸门、
  内存统统不参与。1 个 500 人群刷屏也只相当于 1 人/1.5s。

### 第二层：数据库连接池 5 + `BEGIN IMMEDIATE`（写串行化）

* `Database(path, max_connections=min(5, ...))`：固定 5 条 aiosqlite 连接，
  `asyncio.Queue(maxsize=5)` 借还；拿不到连接 `wait_for(6s)` → `DBPoolTimeout`（可重试业务异常）。
* 读：`fetch_one/fetch_all/fetch_value` 单语句、自动提交，WAL 下**读不阻塞写**。
* 写：`transaction()` 先 `BEGIN IMMEDIATE` 抢 RESERVED 锁，再读-改-写-提交。
  SQLite 全局只有一个写者，所以写天然串行：不会出现"两个事务各持一把读锁同时想升级成写锁"的
  经典死锁（那才是 `database is locked` 的主因）。
* 事务里**禁止**再借第二条连接（重入即死锁），所有 repo 方法都遵守"一个方法一次借还"。
* 明细见 `docs/DB_DESIGN.md`。

### 第三层：渲染闸门 2 + 图片缓存 + GC 节流（内存峰值控制）

* `LimitedRunner(cfg.render_concurrency)`：默认 **1** 路渲染（契约上限 2），排队而不是继续吃内存。
  2 核机器上渲染是纯 CPU，开 2 路已是极限，再高只会互相抢核。
* 渲染一律 `asyncio.to_thread`：Pillow 是同步 CPU 活，放事件循环里会卡住整个机器人。
* `ui/cache.py`：LRU 24 张 + TTL 300s，同一张卡（`Card.cache_key()` 是内容哈希）直接复用已有 PNG 路径。
* 画完 `del img` + `collect_now(2.0)`：渲染一张 800×1200 的 RGB 图约 3.8MB，
  不主动回收会在 2G 机器上把 RSS 顶到危险区。
* 图片落盘在 `plugin_data/<插件>/imgcache/`，但**只保留最近 24 张**且 300s 过期即删；
  内存里不长期持有 `Image` / `bytes`（用完即 `del`），磁盘不会因此膨胀。

三层合起来的稳态：`500 人群高频刷指令 → 防抖砍掉 90% → 5 条连接串行写 → 2 路渲染 → RSS 平稳`。

补充：`core.async_utils` 还提供 `KeyedLock`（按 uid/群号串行化），当某个玩法需要"读-判断-写"
跨多个 repo 调用时（而不是单事务内），service 可用它把同一用户串起来；日常钓鱼不需要。

---

## 5. 为什么这样分层

1. **2 核 2G 的第一约束是内存峰值，不是代码优雅。**
   service 不 import PIL，渲染层才持有 Image；Card 是纯数据（几 KB），
   这样"算数值"和"画图"的生命周期彻底分开，图片只在渲染闸门内存在。
2. **SQL 只在一个文件里，才能做审计与压测。**
   `core/db/repos.py` 是唯一 SQL 出口 → 索引是否命中、有没有全表扫描，
   一个文件就能看完；`scripts/bench_db.py` 也只需要 mock 一个 Repo 入口。
3. **Repo 不 import service，依赖单向。**
   数据层可以被 service / 脚本 / 未来的独立进程复用，不会因为业务改动而牵连数据库。
4. **惰性重置取代定时器。**
   2 核机器最怕"每天 4:00 集体 UPDATE 500 行 + 一堆协程同时醒来"。
   `day_key()` + `settle_user_day()` 把重置摊到每次交互里，代价是一次字符串比较。
5. **错误分两类，处理方式固定。**
   业务错误（WhaleError）= 给用户看的提示卡；系统错误 = 记日志 + 静默。
   这样 AstrBot 核心永远收不到插件异常，插件不会把机器人的消息通道搞挂。
6. **可测、可并行开发。**
   `Card` 协议 + `SvcCtx` 让 5 个 Agent 各写各的文件：service 只依赖协议，
   ui 只依赖 Card，db 只依赖 schema。并行开发不需要互相等编译。