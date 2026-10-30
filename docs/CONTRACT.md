# 赛博鲸鱼馆 · 开发契约书（Team Lead 签发，v1）

> 所有 Agent 动笔前必须先读本文件 + 相关 core/ 源码。**越界改文件=返工**。

## 1. 目标环境（不可妥协）

| 项 | 约束 |
|---|---|
| 服务器 | 2 核 2G 40G，与 NapCat + AstrBot 同机 |
| 绘图 | **只用 Pillow**，严禁 Playwright / HTML 转图 / 浏览器内核 |
| IO | 全部 `async/await`（aiosqlite、asyncio.to_thread），事件循环里禁止同步阻塞调用 |
| 每日重置 | **惰性计算**。禁止 `while True: sleep`、禁止 `asyncio.create_task(每天循环)`、禁止 APScheduler |
| 并发 | DB 连接池 ≤ 5；同时渲染 ≤ 2；单用户指令防抖 1.5s |
| 内存 | 渲染后必须释放 Image 并 `gc.collect()`（有节流）；图片缓存有上限+TTL |

## 2. 目录与归属（禁止跨区改文件）

```
D:\Plugin_Astr\
├─ main.py                 队长：指令层、装配、异常兜底
├─ metadata.yaml / _conf_schema.json / requirements.txt
├─ core/                   队长（A1 只可加固 database.py/decorators）
│  ├─ constants.py         全部数值与颜色
│  ├─ config.py            PluginConfig
│  ├─ context.py           SvcCtx + settle_user_day / grant
│  ├─ db/{database,repos,schema}.py
│  ├─ models.py / rng.py / time_utils.py / async_utils.py / llm.py / paths.py
├─ data/                   物品总表（队长）
│  ├─ items.py             ItemDef 注册表（唯一真源）
│  ├─ fish.py              A3
│  └─ drops.py             A3
├─ services/               A2 / A3 / A4 / 队长(profile.py)
├─ ui/                     A5
│  ├─ schema.py            队长：Card/Block/Row 数据协议（只读）
│  ├─ theme.py / fatfish.py / cache.py / renderer.py   A5
└─ docs/                   A1 架构、A5 审查与数值表、队长避坑指南
```

## 3. 硬性编码规范

1. `from __future__ import annotations` 放文件首行（字符串注解，省内存）。
2. 只允许 `import aiosqlite` / `PIL` / 标准库 / `astrbot.api.*`。不要引 pandas/numpy/requests。
3. service 层**不许 import PIL**，只返回 `ui.schema.Card`。
4. 错误一律 `raise core.errors.WhaleError(...)`；指令层统一捕获转错误卡。
5. 所有金额/概率写进 `core/constants.py` 常量再引用，不许散落魔法数字（可直接在 constants 里补常量，但**只追加，不改别人已用的名字**）。
6. 任何循环总数 > 1000 视为 bug；SQL 必须走索引或 PK。
7. 每个 service 函数都要能重复调用而不破坏数据（幂等或显式状态机）。
8. 模块顶部写 10 行以内的设计说明注释：为什么这样省内存/省 CPU。

## 4. 统一数据访问（`core/context.py::SvcCtx`）

```python
@dataclass(slots=True)
class SvcCtx:
    repo: Repo          # core/db/repos.py，所有 SQL 走这里
    cfg: PluginConfig
    llm: LLMClient      # await ctx.llm.chat(prompt, umo) -> str|None
    plugin_name: str
```
通用助手：`settle_user_day(user) -> bool`（惰性日结）、`settle_harem_day(row) -> bool`、
`await grant(ctx, uid, coins=..., items={...}, exp=...) -> Bag`。

Repo 关键方法（签名以源码为准）：
`get_user(uid, name)` / `update_user(uid, **fields)` / `add_coins(uid, delta)` /
`spend_coins(uid, amount) -> bool` / `add_exp(uid, n) -> (level, exp, leveled)` /
`get_bag(uid)` / `save_bag(uid, bag)` / `mutate_bag(uid, fn)` / `add_items(uid, dict)` /
`consume_items(uid, dict) -> (ok, Bag)` /
`get_pond(gid)` / `donate_materials(gid, uid, dict)` / `take_pond_upgrade(gid) -> (pond, bool, cost)` /
`get_harem(gid, target)` / `ensure_harem(gid, target, name)` / `mutate_harem(gid, target, fn, name)` /
`list_harem(gid, owner, limit)` / `harem_count(gid, owner)` / `affection_ranking(gid, limit)` /
`public_targets(gid, limit)` / `ntr_ranking(gid, limit)` /
`add_buff(uid, kind, duration_sec, group_id, payload)` / `get_buff(uid, kind, group_id)` /
`has_buff` / `clear_buff` / `active_buffs(uid)` / `kv_get` / `kv_set`。

## 5. Service 接口（队长已按此写 main.py，签名不许改）

```python
# services/daily.py
async def do_checkin(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card
async def do_makeup(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card

# services/feed.py
async def do_feed(ctx: SvcCtx, uid: str, name: str, group_id: str,
                  target_uid: str, target_name: str, umo: str) -> Card

# services/work.py
async def do_work(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card

# services/fishing.py
async def do_fish(ctx: SvcCtx, uid: str, name: str, group_id: str,
                  bait_id: str | None = None, umo: str = "") -> Card
async def do_bag(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card
async def do_sell(ctx: SvcCtx, uid: str, name: str, group_id: str, arg: str = "") -> Card

# services/pond.py
async def do_pond(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card
async def do_donate(ctx: SvcCtx, uid: str, name: str, group_id: str, arg: str) -> Card

# services/marriage.py
async def do_marry(ctx: SvcCtx, uid: str, name: str, group_id: str,
                   target_uid: str, target_name: str, use_card: bool = False) -> Card
async def do_divorce(...同 do_marry 形态...)
async def do_ntr(...同 do_marry 形态...)
async def do_harem(ctx, uid, name, group_id) -> Card
async def do_love_ranking(ctx, group_id) -> Card
async def do_public_wives(ctx, group_id) -> Card

# services/shop.py
async def do_shop(ctx, uid, name, group_id) -> Card
async def do_buy(ctx, uid, name, group_id, arg: str) -> Card

# services/items.py
async def do_use(ctx, uid, name, group_id, arg: str,
                 target_uid: str = "", target_name: str = "", umo: str = "") -> Card

# services/pond.py 追加（供 items.py 使用）
async def do_use_pond_card(ctx, uid, name, group_id) -> Card
```

统一返回 `Card`（批量 `card()` + `stat()/kv_block()/text_block()/list_block()/progress_block()/quote_block()/grid_block()` 构造，见 `ui/schema.py`）。

## 6. 文案基线（必须出现的关键词）

- 货币叫 **鲸币**；集团池塘叫 **池塘**；鱼护叫 **鱼护**；排版品牌名 **赛博鲸鱼馆**。
- 道具名严格用 `data/items.py` 里的 `name`，禁止自造同义词。
- 概率/保底必须在文案里明示（例："第 3 次尝试，成功率 30%（5 次内必中）"）。

## 7. 交付前自检清单

- [ ] 所有 await 没有漏（`async def` 内无同步 `sqlite3`/`time.sleep`/`open().read()`）
- [ ] 所有随机走 `core/rng.py`（O(1) Alias）
- [ ] 日重置走 `settle_user_day` / `settle_harem_day`，没有任何定时器
- [ ] 没有 `while True`、没有 `sleep` 轮询
- [ ] 图片由 main.py 统一渲染，service 不碰文件
- [ ] 函数 docstring 写清"消耗/产出/保底规则"