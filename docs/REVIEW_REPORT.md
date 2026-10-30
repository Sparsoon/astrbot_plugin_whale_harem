# 对抗式审查报告（集成轮 · Team Lead + Supervisor）

> 审查对象：`main.py` + `core/` + `data/` + `services/` + `ui/` 全量代码
> 审查方式：逐文件精读 + 跨文件 grep 交叉验证 + **真机 4 套测试**（Python 3.12 / Pillow 12.3 / aiosqlite 0.22）
> 结论：**可以上线**（先 1 个群灰度 3 天）。运行时问题已在真机暴露并修复，详见 [TEST_REPORT.md](TEST_REPORT.md)。

---

## 1. 结论摘要

| 级别 | 数量 | 说明 |
|---|---|---|
| P0（会崩 / 会丢钱 / 会卡死） | 6 | **全部已在集成阶段修复**，见 §2 |
| P1（高概率出问题 / 数值不合理） | 5 | 见 §3，其中 3 条建议上线前处理 |
| P2（体验/可维护性） | 6 | 见 §4 |
| 明确的设计取舍 | 5 | 见 §5，不算缺陷 |

---

## 2. P0：已修复（附修复位置）

### P0-1 `/使用 钟情卡 @TA` 永远拿不到 @ 目标
- 位置：`main.py::cmd_use` + `_run`
- 原因：`_run` 只在 `need_target=True` 时解析 @；`cmd_use` 不能设 `need_target`（补签卡/体验券不需要 @），导致 `target_uid=""`，钟情卡必定抛 `TargetMissing`。
- 后果：功能完全不可用，且用户看到"要 @ 一位群友哦"却明明 @ 了。
- 修复：`_run` 新增 `optional_target=True`（解析但不强制），`cmd_use` 使用它。
- 状态：✅ 已修复。

### P0-2 被墙外目录名加载时整插件 ImportError
- 位置：`main.py` 顶部
- 原因：AstrBot 在某些安装路径下（WebUI 上传会把目录改名为 `plugin_upload_xxx`）以"顶层模块"方式执行 `main.py`，此时 `__package__` 为空，`from .core...` 直接抛 `attempted relative import with no known parent package`，插件加载失败、所有指令消失。
- 修复：在任何相对导入之前注册一个同名合成包并填充 `__package__`（见 `main.py` 第 17~38 行），两种加载方式都能工作；同时补了根 `__init__.py`。
- 状态：✅ 已修复（该兜底不影响正常包导入路径）。

### P0-3 `KeyedLock` 存在 ABA 竞态，可能让同一用户并发执行
- 位置：`core/async_utils.py::KeyedLock.hold`
- 原因：原实现在 release 后 `if not lock.locked(): pop(key)`。当有等待者时 `locked()` 为 False，锁被删除；新协程会创建一把新锁，于是同一 uid 上同时跑两个"读-改-写"→ 签到连击/背包丢物品。
- 修复：改成 `[lock, refcount]`，引用计数归零才 pop，且校验对象身份。
- 状态：✅ 已修复。

### P0-4 渲染落盘不是原子的，并发同 key 可能发出半张图
- 位置：`ui/renderer.py::_render_sync`
- 原因：直接 `img.save(final_path)`；`render_concurrency>1` 或同一张卡同时请求时，NapCat 可能读到写了一半的文件。
- 修复：写唯一 `.tmp` 再 `os.replace` 原子替换。
- 状态：✅ 已修复。

### P0-5 `main.py` 初始化渲染器时同步探测字体，阻塞事件循环
- 位置：`main.py::_ensure_ready`
- 原因：`Renderer.__init__` 会遍历候选字体并 `ImageFont.truetype` 解析（ttc 解析几十毫秒），直接在协程里调用会卡住整个 AstrBot 的事件循环。
- 修复：`await asyncio.to_thread(Renderer, ...)`。
- 状态：✅ 已修复。

### P0-6 `buffs.payload` 写入字符串、读取只保留 int → "写了读不到"
- 位置：`services/items.py`（两处 `add_buff`）
- 原因：`Repo.get_buff` 用 `parse_int_dict` 解析，字符串值被丢弃。当前无人依赖，但会造成"以后想按 payload 判断护盾来源"时静默失效。
- 修复：payload 只写 int（`{"hours": 24}`），并在数据契约里注明。
- 状态：✅ 已修复。

---

## 3. P1：建议处理

### P1-1 图片缓存与渲染的"磁盘残留"（第二轮已修）
- 位置：`ui/cache.py` + `ui/renderer.py`
- 现状：缓存条目/TTL/字节三重限制已生效；渲染器启动时会 `prune()` 一次，清掉上个进程留下的孤儿文件（见 R-5）。
- 残余：`image_cache_size=0`（关闭缓存）时渲染仍会写 PNG，只能等 `close()` 清理。
- 建议：保持默认 `image_cache_size=24`；需要"零磁盘"就设 1（仍可命中当前热点图）。

### P1-2 商店 tier4+ 饵料是 −EV（奢侈品）
- 位置：`data/items.py` 价格 + `data/drops.py` 收益
- 现状：普通饵 8 → 每竿期望 50.0（6.25×）；初级饵 45 → 56.1（+25%）；中级/高级/特级 90/180/360 均为负期望。
- 影响：不是 bug，但玩家买了会觉得"亏"。高品饵的真实来源是钓鱼掉落。
- 建议：接受（反通胀税）；若被抱怨，把 `bait_shop2/3/4` 价格下调 30% 或在商店文案标注"高级饵主要靠钓获"。

### P1-3 单用户每日无上限钓鱼 → 理论印钞
- 位置：`services/fishing.py`
- 现状：只要有钱买饵就能无限钓，日均收入无上限（30 竿 ≈ 1500 鲸币）。
- 影响：长期活跃玩家会迅速买空所有社交动作，婚姻/NTR 的稀缺感下降。
- 建议：二选一（都不改代码结构）：
  1. 把 `bait_normal.price` 从 8 调到 20（ROI 降到 2.5×，最省事）；
  2. 给 `users` 加 `fish_day/fish_count` 字段 + `settle_user_day` 重置，每日前 40 竿正常、之后收益减半。
  **当前版本按"休闲群"定位保留现状**，数值见 `docs/BALANCE.md`。

### P1-4 `harem` 行不会随成员退群清理
- 位置：`core/db/repos.py`
- 影响：长期运行后榜单里会出现已退群的目标（显示 uid 后 4 位）。行数上限 = 群人数，不会爆炸。
- 建议：接受；管理员可手工 `DELETE FROM harem WHERE group_id=? AND target_uid NOT IN (...)`。

### P1-5 公共老婆列表初始为空
- 位置：`services/marriage.py::do_public_wives`
- 原因：不拉取群成员列表（省一次平台 API 调用），只在有人 @ 过之后才有记录。
- 影响：新群第一天 `/公共老婆` 是空的（有引导文案）。
- 建议：接受；若要"开群即有"，需要在指令里调用 `get_group_member_list`（一次平台调用 + 一次写库），收益不高。

---

## 4. P2：观察项

| # | 位置 | 说明 |
|---|---|---|
| P2-1 | `services/pond.py::do_use_pond_card` | 用卡是 `get_pond → save_pond` 两段式，并发用卡理论上会少升一级；已有"满级退卡"补偿，实际概率极低 |
| P2-2 | `core/db/repos.py::public_targets/ntr_ranking` | `ORDER BY chase_count/ntr_count` 无索引；单群 ≤500 行，实测 <1ms，数据涨 10 倍再加 |
| P2-3 | `ui/schema.py::Card.validate` | 硬上限（stats≤4 / blocks≤8 / rows≤12 / lines≤64 字）；service 已按上限裁剪，但"内容过长"时会静默折叠尾部条目 |
| P2-4 | `data/drops.py` | boost 组合数（~192）大于缓存上限（16 档），会 FIFO 重建 Alias 表；每次重建只有 5~8 项，微秒级，可忽略 |
| P2-5 | `main.py::cmd_help/cmd_stats` | 不经过 `_run`，因此不受防抖/群白名单约束（渲染有缓存，风险低） |
| P2-6 | `services/daily.py` | 第 1 天签到 = 60 币（50 基础 + 连续加成 10），与"基础 50"字面略有出入，已在 BALANCE 说明 |

---

## 5. 明确的设计取舍（不是缺陷）

1. **材料按"下一级需求"掉落**：照抄"1-3 基石 / 4-6 珊瑚 / 7-9 鲸骨 / 10 结晶"会让群永久卡在 3/6/7 级（需求表比字面分段快半档）。实现见 `data/drops.py::MATERIAL_TIERS`。
2. **NTR 失败不退还绿帽卡**：卡在掷骰前消耗；文案已明示"失败不退"。
3. **扣费在事务之前**：目标在"校验→扣费→事务掷骰"之间被别人抢走时，本次费用不退（契约允许的"损失一次机会"，换来无跨表补偿逻辑）。
4. **没有任何定时器**：每日 4 点靠 `day_key` + `settle_user_day/settle_harem_day` 惰性结算；过期 buff 在读取时删除、启动时清一次。
5. **私聊不可用**：所有玩法都需要 `group_id`（池塘是群共享的），私聊返回提示卡。

---

## 6. 已验证无问题（附验证方式）

| 项 | 验证方式 |
|---|---|
| 无轮询/定时器 | 全仓 grep `while True`（仅渲染裁剪循环）、`asyncio.sleep`、`create_task`、`APScheduler` |
| 无 Playwright/子进程 | 全仓 grep `playwright/selenium/subprocess` |
| service 不碰 PIL / 不写 SQL | grep 全部 `services/*.py` 的 import 清单 |
| 概率与常量一致 | 对照 `core/constants.py` 逐条核对 `services/marriage.py` 的 rate/pity 公式 |
| O(1) 抽样 | `data/drops.py` 只在 import 与首次 boost 档建表；主路径只 `.pick()` |
| 连接池上限 5 | `Database(max_connections=5)` + `min(5, ...)` 夹紧 |
| 事务单连接 | 逐个 repo 方法检查：无嵌套 `async with self.connection()` |
| 图片缓存三重上限 | `ui/cache.py` 条目/TTL/字节三条淘汰路径 |
| 渲染在 to_thread | `ui/renderer.py::render` 唯一入口 |
| 签名对齐 | `main.py` 21 条指令逐个对照 service 函数签名 |

---

## 7. 上线前必须人工确认的 3 件事

1. ~~跑一次 smoke_test~~ **已跑通**：真机 4 套测试全绿（`smoke` 36 项 / `integration` 26 项 / `edge` 19 项 / 渲染 8 张）。
   上线前仍建议在目标机再跑一遍 `python scripts/smoke_test.py`（验证该机的 Python/Pillow/字体环境）。
2. **确认服务器装了中文字体**（`fonts-noto-cjk` 或配置 `render_font_path`），否则图片里的中文是方块。
3. **确认时区**：`TZ=Asia/Shanghai`，否则"凌晨 4 点重置"会落在 UTC 4 点（北京时间 12 点）。

---

## 8. 渲染层 API 自查（Pillow ≥ 10）

| 调用 | 用法 | 结论 |
|---|---|---|
| `ImageFont.truetype(path, size)` | 字体探测链 + 失败回退 `load_default(size)` / `load_default()` | ✅ 两层 try/except，缺字体不会崩 |
| `font.getbbox(text)` | `theme.text_size`，兼容旧版 `getsize` | ✅ |
| `Image.Resampling.BILINEAR` | `vertical_gradient` 用 1×H 小图拉伸 | ✅ Pillow≥9.1；requirements 锁 ≥10 |
| `draw.rounded_rectangle(radius=, width=)` | `theme.rounded_rect` | ✅ 保留 `rectangle` 兜底（<8.2） |
| `draw.text(anchor=, stroke_width=, stroke_fill=)` | `theme.draw_text` | ✅ `ValueError/TypeError` 时退化为无 anchor 绘制 |
| `draw.ellipse/arc/line/polygon(width=)` | `fatfish.py` 全部图元 | ✅ 均 5.3+ 支持 |
| 测量/绘制一致性 | `_paint_block(draw=None)` 与 `_paint_block(draw)` 共用同一套 x/width（`T.PAD` + `T.INNER_WIDTH`） | ✅ 测量与绘制同函数，不会"文字出框" |
| 二次建图风险 | 渲染前先跑 `_total_height` 再 `Image.new` | ✅ 不存在"先建大图再裁" |
| 并发写同一缓存文件 | 唯一 `.tmp` + `os.replace` | ✅ 原子替换 |
| 图像引用释放 | `_render_sync` / `_render_bytes_sync` 的 `finally` 里 `del img` + `collect_now()` | ✅ |

> 说明：`ui/theme.py` 在绘制前会剥掉 emoji（CJK 字体无彩色字形，直接画会变豆腐块），
> 并保留 `★☆♥` 等常用符号；纯文本兜底 `Card.to_text()` 不过滤，QQ 侧 emoji 正常显示。

---

## 9. 第二轮复核（Supervisor）与处置

第二轮针对渲染层 API、报告纠错与数值复算做了独立复核，产出 `docs/REVIEW_ROUND2.md`。
独立复算的 Lv1 单竿期望 = **49.98**（与 BALANCE 的 50.0 相差 0.04%），互证通过。

| # | 复核意见 | 定级 | 处置 |
|---|---|---|---|
| R-1 | `theme.draw_text` 的 except 用同一个非法 fill 重画，第二次仍抛 ValueError | P0 | ✅ 已修：新增 `normalize_color()`（品质中文名→hex、无 # 的 hex、元组），并做"去 anchor → 去颜色"两级兜底 |
| R-2 | `services/profile.py` 把 `"小金"/"小紫"` 当 accent 传入 | P0 | ✅ 已修：改传 `C.RARITY_COLORS[...]`；渲染层同时兜底，双保险 |
| R-3 | `renderer._total_height` 返回 `min(total, 1600)` → 截断循环永不触发，长卡画到图外、页脚压内容 | P1 | ✅ 已修：返回真实高度，仅在建图时 clamp；裁剪循环与"内容过长"提示恢复正常 |
| R-4 | `cache.put` 对同 key 先删文件再登记 → 并发同 key 时删掉刚写的图，消息无图 | P1 | ✅ 已修：同路径只更新索引（size/时间/LRU），绝不 unlink |
| R-5 | `cache.prune()` 全仓无调用方，缓存关闭时磁盘只增不减 | P2 | ✅ 已修：`Renderer.__init__` 启动时 `prune()` 一次（不是轮询） |
| R-6 | `rounded_rectangle(radius=3, height=5)` 在 Pillow 10 可能抛 ValueError | P0（若抛） | ✅ 已修：radius 夹紧到短边一半以内，并捕获 ValueError 退化为矩形 |
| R-7 | `renderer.render` 在事件循环里调用 `cache.get`（含 stat 与锁） | P2 | ✅ 已修：改为 `asyncio.to_thread(self._cache.get, key)` |
| R-8 | `main.initialize` 无 try，DB 初始化失败会让插件加载失败 | P1 | ✅ 已修：try/except + 30 秒重试退避，指令层提示"维修中" |
| R-9 | `/鲸鱼馆` 用 `seconds_to_reset()` 做页脚 → 缓存键每秒变 | P2 | ✅ 已修：重置时间取整到分钟 |
| R-10 | 喂食返币"第二台印钞机" | P0（复核口径） | ⚠️ 部分采纳：里程碑每日仅一次，不存在"每分钟 +628"（那需要反复触发里程碑）；但米饭确实偏便宜 → `rice_bowl.price` 12→15，第 6 次起每次约 −2.5，里程碑仍 +64/5 次。BALANCE 已按新价重算 |
| R-11 | 材料口径：公式写"含材料"但表格未计入 | 文档 | ✅ 已修：表头标注"不含材料"，并补材料残值（Lv1 +2.2/竿，Lv10 +27/竿） |
| R-12 | 钓鱼无每日上限（P1→P0？） | 争议 | 维持 P1：与"点击次数"强相关，任何指令都可用防抖压住；旋钮已写入 BALANCE（`bait_normal.price` 8→20 可把 ROI 砍半） |
| R-13 | 升塘卡 `get→save` 两段式可能吞一级 | P1（复核建议） | 维持 P2：已有"满级自动退卡"补偿，且 `save_pond` 紧邻读取、无中间 await，窗口仅一次 IO |
| R-14 | `_paint_block` 的 list 前缀可能超宽 6px | P2 | ✅ 已修：折行宽度预留 18px |

> 第二轮同时标注了 3 项"需运行时确认"：Pillow 对超大 radius 的行为（已用 clamp 消除）、
> `profile` 的 accent（已修）、`feed` 是否已改（已改）。

---

## 10. 真机实测结果（本轮新增）

| 测试 | 结果 |
|---|---|
| `compileall -f .` | 0 error |
| `smoke_test.py` | 36 项断言全过 |
| `integration_stub_test.py`（真实 main.py + astrbot stub） | 26 项全过：21 条指令、@ 解析、防抖、白名单、私聊、错误卡、terminate |
| `test_edge_cases.py` | 19 项全过：缓存淘汰/同路径/TTL、跨天重置、保底概率、顶层模块加载、缓存命中 |
| `bench_db.py -n 20 / -n 200` | 0 错误、idle=5/5 无泄漏；P50 11ms / 147ms，P95 59ms / 772ms |
| `render_demo.py` | 8/8 成功；单张 63~109ms；**峰值 WorkingSet 32.2MB**；缓存 446KB |

真机实测额外暴露并修复：错误卡 hint 重复、签到插画重叠、`@register` 未来兼容、`Star.__init__`
在 4.9/4.29 的签名差异、测试脚本异常时 aiosqlite 非守护线程挂住。完整记录见
[TEST_REPORT.md](TEST_REPORT.md)。