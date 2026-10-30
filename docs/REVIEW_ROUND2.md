# 第二轮审查（第二意见）· 渲染 API / 报告纠错 / 数值复核

> 审查范围：`docs/REVIEW_REPORT.md`、`docs/BALANCE.md`、`ui/{renderer,theme,cache,fatfish,schema}.py`、`main.py`、`data/{drops,fish,items}.py`、`core/rng.py`
> 方式：精读 + 手算，**无运行时**；不确定处已显式标注。

## 1. 渲染层 API 正确性（Pillow ≥10）

### theme.py
| 项 | 结论 | 说明 |
|---|---|---|
| `ImageFont.truetype(path, size)` | ✅ | `theme.py:107,130`；只捕 `OSError`，字体文件坏/缺子字体 Pillo 也抛 OSError，够用 |
| `font.getbbox(text)` | ✅ | `theme.py:108,237`；Pillow≥8 的 FreeTypeFont 有 getbbox；异常有 `getsize`/估算兜底 |
| `Image.Resampling.BILINEAR` | ✅ | `theme.py:206`；Pillow≥9.1 才有，`requirements.txt` 锁 ≥10 安全 |
| `rounded_rectangle(width=)` | ⚠️ 不确定 | `theme.py:193` 参数名正确（≥8.2）；但 **radius 未 clamp**。`renderer.py:272` 用 `(x0,y,x1,y+5), radius=3`（3 > 5/2）。Pillow 10 各小版本对超大 radius 是否抛 ValueError **需运行时验证**；若抛则所有卡片降级文字（会升 P0） |
| `draw.text(anchor/stroke_width/stroke_fill)` | ✅ 参数合法，❌ 兜底有洞 | 参数 ≥5.3 支持；但 `theme.py:263-264` 的 except 里**用同一个非法 fill 重画一次**：`renderer.py:387` 传 `item.accent`，若 service 传的是品质中文名（如 `C.RARITY_XIAOJIN`，上轮在 `services/profile.py::do_profile` 读到），`fill="小金"` → ValueError → 第二次仍 ValueError 逃逸。**不上运行时无法确认 profile.py 是否已改；若未改，`/我的` 永远文字降级** |
| 文字清洗 | ✅ | `sanitize` 在 `text_size/wrap_text/draw_text` 内做，测量与绘制同源；`ellipsis` 只测量不清洗，但 `draw_text` 会清洗，宽度只会更小，不越界 |

### renderer.py（测量 vs 绘制）
- ✅ **同一坐标系**：`_block_height` 用 `_paint_block(draw=None, x=T.PAD, y=0, width=T.INNER_WIDTH)`；`_draw_block` 用 `x0=T.PAD, y+BLOCK_PAD, width=x1-x0=T.INNER_WIDTH`。两者 `inner_x=T.PAD+16`、`inner_w=632` 完全一致（`renderer.py:394,402,421-422`）。
- ✅ 测量/绘制同公式：两遍都走 `_paint_block`，返回值都是 `cursor-y`，面板高度 `content+32`，不会"面板不够高"（`renderer.py:393-406`）。
- ❌ **截断永不触发**：`_total_height` 末尾 `return min(total, T_MAX)`（`renderer.py:235`），于是 204 行 `if total <= T_MAX` 永远成立。超过 1600px 的卡片**不会折叠块**，而是继续画到图外（被裁掉），且 221 行 `truncated` 恒 False（"内容过长"提示永不出现）；`_draw_footer` 固定在 `total-38`（1600-38），会**压在最后几个块上**（`renderer.py:201-208,221-223,534`）。缓存键却包含被裁掉的块（`schema.py:110-112`）→ 同图多键。
- ⚠️ **list 前缀溢出**：`wrap_text` 用 `inner_w-14` 折行，绘制时再拼 `"· "`（`renderer.py:438,445`）。632-14=618 的正文加 ~20px 前缀 → 最多越出 6px，仍在 16px 内边距里，不会画出面板，但行宽不严格等于测量值。
- ✅ **grid 奇数行**：`cursor = row_cursor+46` 在 `index==len-1` 时执行，最后单行也留满 46px，测量/绘制一致（`renderer.py:493-515`）。
- ✅ **空块/空行**：`rows=[]` 返回 0 → `_block_height` 取 `STAT_MIN_H=44`；空 body 行 `wrap_text` 返回 [""]，两遍都按 0 宽绘制，不抛错。
- ⚠️ **超长英文单词**：`wrap_text` 的 ASCII 分支可能产出一行略宽于 `max_width`（`theme.py:300-306`），但 `Card.validate` 限 64 字符，16px 下约 480px < 632px，实际碰不到。
- ⚠️ **block 间距只算不画**：`_total_height` 每块 +GAP，`_draw_block` 之间不加 GAP（`renderer.py:219-220,232-233`）→ 面板圆角相接、页脚前留白。视觉 P2。
- ✅ **Image/Draw 释放**：`_render_sync` finally `del img` + `collect_now`；`_compose` 异常时靠引用计数回收，无实例/模块级 Image。

### cache.py
- ✅ `OrderedDict + threading.Lock`：get/put/淘汰/enforce 全在锁内，索引不会撕裂；`safe_unlink` 也在锁内（短）。
- ❌ **put 会删掉自己刚写的文件**：`put` 先 `_drop_locked(key, old)`（内含 `unlink(entry.path)`），而 renderer 的路径恒为 `dir/{cache_key}.png`（`renderer.py:165-166`），同 key 必然同路径。并发/排队同 key 渲染时，后一次 `put` 把前一次（也是自己）刚 `os.replace` 的文件删掉，随后登记一个不存在的路径并返回 → **主程序把已删路径交给 `image_result`，消息无图**（`cache.py:117-124,127-130`）。
- ⚠️ **淘汰删"正在返回"的路径**：get 解锁后才把 `Path` 交给调用方，随后别的线程 put/enforce 可 LRU 淘汰并 unlink 该路径；24 条缓存 + 发送窗口，概率低但存在（`cache.py:105,144-147`）。
- ❌ **ttl=0 / size=0 时缓存"禁用"但渲染仍落盘**：`enabled` 要求 `ttl>0`（`cache.py:82`），`put` 直接 return；renderer 仍按 key 写唯一 PNG（`renderer.py:164-181`），而 `prune()` 全仓无调用方（仅 `clear()` 在 close）。配置成 0 时文件只增不减。
- ⚠️ TTL 用 `created` 而非 mtime，命中不续期（与契约"touch mtime"不一致，但更可控，不算 bug）。

### fatfish.py ↔ renderer._draw_art
- ✅ 全部签名/参数顺序一致：`draw_fish(img, draw, box, star, accent)`、`draw_pond_scene(..., level)`、`draw_hearts/shop/sweat/calendar/bag/error_face(img, draw, box)`、`draw_fatfish(img, draw, box, mood=, accent=)`（`renderer.py:324-351` ↔ `fatfish.py:489-786`）。
- ⚠️ `renderer.py:321` 把 fish 星数夹到 1..5，而 `fatfish.draw_fish` 支持 1..8 品质色；`"fish:7"` 会画成 5 星配色（`fatfish.py:267-277`）。
- ⚠️ `renderer.py:327-331` 从 `badge` 正则取 pond level；`/鲸鱼馆` 的 pond 卡无 badge → 永远 Lv1（仅装饰）。

## 2. 对 REVIEW_REPORT 的补充 / 纠错

**漏掉的 P0（1 条）**
1. **喂食返币无每日上限 = 第二个印钞机** — `services/feed.py::do_feed`（上轮读到的 147-158 行；本轮未重读该文件，若已改请降级）：每碗饭返 5~20（均值 12.5），第 5 次里程碑 +60 + 2 饵（16）；软上限只砍好感、**不砍鲸币**。5 次净收益 = −60 +62.5 +60 +16 = **+78.5（+15.7/次）**，1.5s 防抖下 ≈ +628/分钟。REVIEW_REPORT 只覆盖钓鱼，BALANCE 第 74 行写"喂食返币（5 次）+25"，**低估约 3 倍**。

**定级不合理**
- `P1-3 单用户无上限钓鱼`：若按"印钞漏洞= P0"的口径应升 P0；且它只提"30 竿≈1500"（实为 +1260 净），没提喂食这条更隐蔽的。
- `P2-1 pond 升塘卡并发`：`get_pond → save_pond` 两段式（`services/pond.py::do_use_pond_card`）会吞卡/吞一级，稀有道具真丢，建议 P1。
- `P1-1 缓存残留`：低估——不是"缓慢增长"而是"配置为 0 时永不清、且 prune 无调用方"，建议补"启动时 `cache.prune()`"一行。

**其他漏项（P1）**
- 渲染截断失效（§1，`renderer.py:201-208,235`）：长卡尾部数据静默丢失 + 页脚压内容。
- `cache.put` 自删（§1，`cache.py:117-124`）：并发同 key 时消息无图；修法一行：`old.path == target` 时只更新 size/created，不 unlink。
- `main.py::initialize` 直接 `await self._ensure_ready()` 未 try（`main.py:92-93`）：DB 初始化失败会抛给 AstrBot 导致插件加载失败，与文件头"失败也不阻塞启动"注释相反（`_run` 里的 try 只能救之后的指令）。
- `renderer.render` 在**事件循环**里调 `self._cache.get(key)`（`renderer.py:114`，含 `stat` 与锁）；报告"渲染在 to_thread"只对绘制成立，缓存命中路径仍有同步 IO，慢盘上是事件循环抖动源。
- `main._stats_card` 的 footer 用 `seconds_to_reset()`（每秒变）→ `/鲸鱼馆` 缓存键每秒不同，等于关掉该卡缓存（`main.py` `_stats_card`，报告 P2-5 只提防抖）。

**维持原判**：P1-2（tier4+ −EV）、P1-4、P1-5、P2-2、P2-4、P2-5（防抖部分）、P2-6 无异议；P0-1~P0-6 的修复当前代码可见且正确。

## 3. BALANCE 数字复核

**Lv1 单竿（普通饵、Lv1 池、无券）独立重算**
- 上钩率 = 0.72 + 0.02 = **0.74**（实现口径，`drops.py:137-139`）
- boost = 0.08；STAR_WEIGHTS×1.08^rank → 鱼 E = 0.3676×24.5 + 0.2978×60.6 + 0.1929×123.4 + 0.1042×263 + 0.0375×680 = **103.8**
- 道具 E = (30×40 + 28×40 + 24×40 + 18×30)/100 = **38.2**
- 物品 E（品质×1.04^rank，池内等分）= **24.4**
- 每竿 = 0.74 × (0.5×103.8 + 0.25×38.2 + 0.25×24.4) = **49.98**
- 材料残值 = 0.74 × 1.5 × 2 = **2.22** → **52.20**（含材料）

与 BALANCE 的 **50.0 相差 0.04%**（不含材料），结论一致；但 BALANCE 的公式行写了"+ 材料残值"、表格 50.0 却没加（差 2.2，4.4%，<10%），属**文档内部不一致**而非算错。若按含材料口径，日净收入应再 +0.74×1.5×材料价×竿数。

**125 材料 ≈ 113 竿**
- 125 ÷ (0.74×1.5=**1.11 个/竿**) = **112.6 ≈ 113** ✅ 与 BALANCE 吻合。
- 但 1.11 用的是 Lv1 上钩率；升级过程中钩率 0.74→0.92，按各级平均 ≈0.83 算应为 **125÷1.245 ≈ 100 竿**。113 是保守值，不算错。
- 各级需求与 `MATERIAL_TIERS` 对账：22 基石/26 珊瑚/54 鲸骨/23 结晶；Lv7→8 的 24 骨:3 结晶与掉落权重 24:3 同比例，不会卡级 ✅。材料卖掉（2/6/10/25）未计入 BALANCE，属口径选择。

**最该调的 1 个旋钮**
> `data/items.py::rice_bowl.price` **12 → 30**。
> 理由：钓鱼的 42/竿 已被 BALANCE 记账并有旋钮；喂食 +15.7/次是**未被记账且无上限**的第二台印钞机。12→30 后单次喂食约 **−5.5 鲸币**（好感仍是产出），商店物价与 `sell=4` 不变、无套利，一行改动即封口。
> （若你只允许动钓鱼，则退而求其次：`bait_normal.price` 8→20，Lv1 ROI 6.25×→2.5×，日净约砍半。）

---
**不确定项（需运行时）**：Pillow 10 对 `rounded_rectangle` 超大 radius 是否抛错；`services/profile.py` 的 stat accent 是否仍为中文品质名；`feed.py` 的返币逻辑本轮未重读是否已改。