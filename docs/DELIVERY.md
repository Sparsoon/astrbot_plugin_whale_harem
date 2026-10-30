# 交付说明（Team Lead）

> 本文件是**交接单**：怎么装、怎么验、哪些是取舍、出问题先看哪里。

## 1. 交付清单

| 类别 | 文件 | 说明 |
|---|---|---|
| 插件入口 | `main.py` | 22 条指令、参数解析、防抖/串行/超时、异常兜底、统一出图 |
| 元信息 | `metadata.yaml`、`_conf_schema.json`、`requirements.txt`、`__init__.py` | AstrBot 插件包 |
| 核心 | `core/` | 常量、配置、上下文、DB 池/仓库、Alias 随机、时间（惰性 4 点）、LLM 桥、装饰器 |
| 数据 | `data/` | 物品总表（8 品质）、26 种鱼（5 星级）、掉落池与 O(1) 采样表 |
| 玩法 | `services/` | 签到/补签、喂食、打工、钓鱼、鱼护/卖鱼、池塘/捐献、娶/离/NTR/后宫/榜单、商店、道具、面板 |
| 渲染 | `ui/` | Card 协议、主题/字体、LRU+TTL 缓存、Pillow 渲染器、大肥鱼插画 |
| SQL | `sql/schema.sql` | 6 表 / 3 索引 / WAL PRAGMA / JSON 字段契约 |
| 文档 | `docs/` | 契约书、架构、DB 设计、存储审查、防崩溃指南、数值平衡、审查报告、实测报告 |
| 脚本 | `scripts/` | `smoke_test`（36 断言）、`integration_stub_test`（26）、`test_edge_cases`（19）、`bench_db`、`render_demo`、`sim_economy` |
| 预览 | `docs/preview/*.png` | 真机渲染的 8 张卡片（签到/钓鱼/池塘/成婚/商店/面板/帮助/错误） |

## 2. 怎么验证（已真机跑通，3 分钟复现）

```bash
cd <AstrBot 插件目录>/astrbot_plugin_whale_harem
pip install -r requirements.txt

# 1) 离线冒烟：不需要 AstrBot / QQ，跑完 12 个玩法流程（36 项断言）
python scripts/smoke_test.py

# 2) 真实 main.py 集成：内置最小 astrbot stub，走 22 条指令（26 项断言）
python scripts/integration_stub_test.py

# 3) 边界回归：缓存/跨天重置/保底概率/顶层加载（19 项断言）
python scripts/test_edge_cases.py

# 4) 存储压测：并发 + 连接池泄漏检测
python scripts/bench_db.py -n 20

# 5) 出图预览：8 张典型卡片 -> docs/preview/
python scripts/render_demo.py

# 6) 真实环境：把目录放进 AstrBot 插件目录 -> 重载插件 -> 群里发 /帮助
```

本机实测结论（Python 3.12 / Pillow 12.3 / aiosqlite 0.22）：4 套测试全绿、8 张图渲染成功、
峰值内存 32.2 MB、DB 20 并发 P50 11 ms。完整数据见 [TEST_REPORT.md](TEST_REPORT.md)。

### 2.1 生产部署记录（2026-10-25，阿里云 Docker）

```bash
# 宿主机：Ubuntu 22.04.5 / 2C / 1.7G / 40G，AstrBot 跑在 Docker 里
# 容器：astrbot（soulter/astrbot:latest），/root/astrbot/data -> /AstrBot/data
# 插件文件：/root/astrbot/data/plugins/astrbot_plugin_whale_harem
# 数据：    /AstrBot/data/plugin_data/astrbot_plugin_whale_harem/whale.db（WAL）
# 配置：    /AstrBot/data/config/astrbot_plugin_whale_harem_config.json（AstrBot 自动生成）

# 部署（已在 2026-10-25 20:24 执行）
docker restart astrbot        # 约 10~20 秒；NapCat 容器不受影响
```

部署后容器日志（实测）：

```
Loading plugin astrbot_plugin_whale_harem ...
Plugin astrbot_plugin_whale_harem (1.0.0) by 煤球: ...
鲸鱼馆数据库就绪: /AstrBot/data/plugin_data/astrbot_plugin_whale_harem/whale.db (连接数 5, WAL)
鲸鱼馆中文字体: /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
赛博鲸鱼馆启动完成 | llm=True cache=24 render=1 debounce=1500ms
```

**回滚**（一条命令，不影响其他插件）：

```bash
rm -rf /root/astrbot/data/plugins/astrbot_plugin_whale_harem
docker restart astrbot
```

### 2.1.1 运维注意（实测踩坑）

1. **备份不要放插件目录**：`cp -a` 到 `/root/astrbot/backups/`，不要放 `data/plugins/` 里，
   否则 AstrBot 会把 `*.bak-*` 当插件扫描并报加载错误。
2. **插件的启用/禁用状态**不在 `cmd_config.json`，而在 `data_v4.db` 的 `preferences` 表
   （key = `inactivated_plugins`，value 是 `{"val": ["data.plugins.xxx.main", ...]}`）。
   WebUI 里点"禁用"会写这里；命令行救急可停容器后改该行再启动。
3. 插件初始化失败时**不会让 AstrBot 崩**：日志出现"初始化失败（插件仍加载…）"，30 秒后自动重试；
   先看 `whale.db` 是否存在、`PRAGMA table_info(users)` 列是否齐全。

### 2.2 v1.1 变更（2026-10-25 晚）

* **形象替换**：弃用手绘大肥鱼，改用 DeepSeek 社区形象（`assets/mascot_q.jpg` + `assets/mascot_2d.jpg`）：
  普通面板用 Q 版贴纸，资料/帮助/池塘用 2D 立绘；素材缺失时自动回退手绘。
* **钓鱼改两段式**：抛竿消耗 1 饵 → 随机等待 **5~60 分钟**（与渔获无关）→ **到点后由群聊随机触发咬钩**
  （每条群消息 25% 概率，监听器惰性检查、零轮询）→ 自动出结算图；等待期间不能再钓鱼。
* **打工改两段式**：上工随机 20~180 分钟 → **到点后本人发一条群消息自动收工领工资**；
  **收益与等待时长成正比**（时长 × 0.9 × 浮动 ±25%，12% 暴击 ×1.8）。钓鱼等待中同样不能打工。
* **新增 `/仓库`**：按类别查看全部物品、数量与回收估值。
* 数据库 **v1 → v2**：users 表自动补 7 列（work_started_ts / work_minutes / work_pending_coins /
  fish_ready_ts / fish_bait / fish_wait_min / fish_group），启动时幂等 `ALTER TABLE`，老库无损升级。

### 2.2.1 v1.2 变更（2026-10-26）

* **钓鱼 / 打工解耦**：两者不再共享"忙"状态，可以一边等鱼一边上班；
  各自只限制自己（钓鱼等待中不能再次抛竿，打工等待中不能再次上工）。
* **喂食改为喂 bot**：`/喂食` 不再要求 `@群友`，改为投喂鲸鱼娘本人，
  好感记在 `users.bot_affection`，与 `harem` 表完全解耦；娶群友成功率只与失败次数有关。
* **婚姻系统自洽**：`harem.affection` 改名为"恩爱值"，每天自然 +2（惰性结算、封顶 999），
  不再靠喂食刷；恩爱榜按恩爱值排序。
* **私聊支持**：签到/喂食/钓鱼/打工/仓库/商店/使用/我的/帮助可在私聊使用，
  池塘用 `private:<uid>` 独立作用域；钓鱼咬钩与打工收工在私聊同样由消息触发。
* 数据库 **v2 → v3**：users 表补 `bot_affection` 列（同样幂等迁移）。
* 新增 `scripts/xshell_run.ps1`：通过 XShell 窗口粘贴命令 + 截图读结果，测试服务器不再每次弹密码框。

### 2.2.2 v1.3 变更（2026-10-26 晚）

* **天气系统**：7 种天气（晴/多云/小雨/雷雨/大雾/流星雨/寒潮），每 6 小时自动变化（稳定哈希，零轮询）；
  同时影响等待倍率、上钩率、物品品质偏移、鱼星偏移、鲸币袋倍率、材料数量，每种天气都有 1 增益 + 1 负面。
* **提杆逻辑重做**：抛竿 → 到点后**本人再发消息**（65%/条）才提杆；不再由任何群友的消息触发。
  上钩率 = 基础 0.72 + 池塘（或体验券固定 Lv.5）+ 券 + 饵料 tier + 天气，钳 [0.45, 0.96]。
* **体验券池塘**：按固定 Lv.5 计算上钩率与品质，**不吃实际池塘等级**，但天气照常生效。
* **品质曲线**：蓝 70% / 紫 24% / 金 4.8% / 红 0.8%，其中**大红 0.2%**；
  鱼星 1★52% / 2★30% / 3★13% / 4★4% / 5★1%；池塘等级给最低品质保底（Lv1-3 小蓝、4-6 大蓝、7-9 小紫、Lv10 大紫）；
  天气正向偏移最多推到小红，不能直接送大红。
* **新道具（均不可购买）**：天气控制机（大红，改 3 天天气）、发光饵（小红，Tier5）、星空体验券（小金，24h 鱼星 +1）、
  潮汐铃（大紫）、深海珍珠/幸运护符（小紫）。
* **签到长线**：连签 1 天 50 → 20 天 100，封顶 100/天。
* **喂食改版**：每天早/中/晚 3 次（5-10 / 10-16 / 16-22 点），累计里程碑 10/20/40/80/160 次一次性奖励；
  台词 prompt 重写（傲娇但心软、25 字内），本地语料兜底。
* **卡片署名**：所有卡片标题下方显示「群友 · 昵称」，避免多群/多用户混淆。
* 数据库 **v3 → v4**：users 补 `feed_slots/feed_total/feed_milestone`，ponds 补 `weather/weather_until`。

### 2.2.3 v1.4 变更（2026-10-28）

* **喂食经济修正**：白米饭 15→8、铁盆 40→22；喂食返币 8~24，最差情况也不亏鲸币。
* **打工长线化**：时长 20~180 分钟 → **60~1440 分钟（1 小时~1 天）**；单价 0.9→0.35/分钟，
  1 小时约 21 鲸币、24 小时约 504（暴击 ×1.8）。
* **喂食自动购买**：`/喂食 自动 白米饭` / `/喂食 自动 铁盆饭` / `/喂食 关闭自动`；
  背包没饭时按偏好自动扣鲸币购买，结算卡显示来源与花费。DB v4→v5（`feed_auto_food`）。
* **错误面板重做**：错误卡现在分「原因 / 下一步怎么做 / 正确示例」三层，并按指令给出示例；
  超时、内部异常、背包不足、鲸币不足、缺 @ 都有可执行引导。
* **池塘升级材料扩充**：新增贝壳（小紫）、琥珀（大金），6 种材料组合；
  升级门槛整体上调（如 Lv8 要 24 鲸骨 + 8 琥珀 + 3 结晶），掉落按"下一级需求"动态加权。
* **文案清理**：修掉所有 `/鲸鱼xxx` 旧指令名、过期的"20~180 分钟"、错误的互斥说明与无用 footer。
* **花园预构建**：`data/plants.py` + `services/garden.py` + `docs/GARDEN_DESIGN.md` 骨架，
  未注册指令、未建表、不影响现有玩法。

### 2.2.4 v1.5 变更（2026-10-30）

* **花园（养植物）上线**：S/A/B/C/D/E 六档种子、7 档植物品质（小蓝→星彩）、
  播种/发芽/幼苗/营养生长/开花/结果 6 阶段；品质在发芽后才揭晓。
* **四种操作**：播种（选种子）、浇水（1 小时 1 次，面板窄）、施肥（2 次/天，氮/磷/钾侧重不同）、
  晒太阳（1 次/天，7-19 点可配时区，收益随天气），收获果实 + 一颗种子。
* **突变值**决定收获种子档位（0→最差、100→该品质最好），达到该品质上限后不再增长；
  品质与种子区间严格按 星彩 S / 大红 S-A / 小红 S-B / 大紫 S-C / 小紫 A-C / 大蓝·小蓝 B-E。
* **状态系统**：缺水（2 天，收益 -50%）、缺氮/磷/钾（3 天，各 -15%）、缺光（2 天，-30%）、
  5 天不浇水枯萎；状态只影响后续收益，不倒扣已有成长。
* **经济闭环**：D/E 种子可在商店买（8/4 鲸币），钓鱼有 12% 概率掉种子（S 极低），
  果实可卖或加空碗/空盆做「果实餐」喂鲸鱼娘；空碗 2 鲸币、空盆 6 鲸币。
* **天气联动**：晒太阳收益随当前天气（含天气控制机覆盖）变化。
* 数据库 **v5 → v6**：新增 `gardens` 表。
* 商店排版修正：商品名放大加粗，品质/价格改小字；天气改为 **12 小时**（半天）一换。

## 3. 已验证 / 未验证

| 项 | 状态 |
|---|---|
| 全量语法 / 导入 | **已实测**：`python -m compileall` 0 error（本机 + 容器内各一次） |
| 玩法链路（签到→钓鱼→社交→商店） | **已实测**：`smoke_test.py` 36 项断言全过（本机 + Ubuntu 服务器各一次） |
| 22 条指令装配 / @ 解析 / 防抖 / 白名单 / 错误卡 | **已实测**：`integration_stub_test.py` 26 项全过（真实 main.py + astrbot stub） |
| 惰性跨天重置 / 保底概率 / 缓存三重限制 | **已实测**：`test_edge_cases.py` 19 项全过 |
| Pillow 真实出图 | **已实测**：Windows 8 张（峰值 32.2 MB）；Ubuntu 容器 8 张（16~26 ms/张，中文正常） |
| DB 并发与连接泄漏 | **已实测**：20 并发 P50 11ms；200 并发 P95 772ms；idle=5/5 |
| AstrBot 4.29.3 API 签名 | **已核对**：解包官方 wheel 逐项比对（见 TEST_REPORT §3） |
| **真实 AstrBot 进程内加载** | **已实测**：生产容器里插件注册、建库、字体探测、启动完成，容器内存 373 MB |
| **Linux + Noto CJK 的排版** | **已实测**：容器内渲染 8 张，中文正常（见 `docs/preview/linux_help_font.png`） |
| 概率与保底实现 | 已验证（公式与 `core/constants.py` 对照 + 单测断言 + 20 万竿蒙特卡洛） |
| 无定时器 / 无轮询 / 无 Playwright | 已验证（全仓 grep） |
| service 不 import PIL / 不写 SQL | 已验证（grep import 清单） |
| **真实 QQ 群里的图文交互** | **未验证**：插件已上线，但还没在群里发过指令（需要你发一条 `/帮助`） |
| **真实 LLM 喂食台词** | **未验证**：容器里 `llm=True`，但未触发喂食 |

## 4. 已知取舍（都是有意为之）

1. **材料掉落按"下一级需求"推导**，而不是字面的「1-3 基石 / 4-6 珊瑚 / 7-9 鲸骨 / 10 结晶」。
   原因：`POND_UPGRADE_REQUIREMENTS` 涨得比字面分段快半档，照抄会让群永久卡在 3/6/7 级。
   实现见 `data/drops.py::MATERIAL_TIERS`（注释里写了推导）。
2. **签到第 1 天是 60 鲸币**（50 基础 + 连续加成 10），7 天封顶 120。
3. **公共老婆** = 群里尚未被娶走且有过互动记录的目标 + 被 NTR 最多的榜单（不拉群成员列表，省一次平台 API 调用）。
4. **打工即时结算**，冷却只是时间戳比对（"随机时长"决定收益与冷却），进程里不 sleep。
5. **商店的 tier4+ 商品饵是奢侈品**（−EV），高品饵主要靠钓鱼掉落——这是刻意的反通胀设计。
6. **鱼护溢出**会按估价自动卖掉最旧的鱼并在卡片上说明，防止 JSON 无上限膨胀。

## 5. 出问题先看哪里

| 现象 | 先查 |
|---|---|
| 图片里中文是方块 | 装 `fonts-noto-cjk`，或在配置 `render_font_path` 指定 ttf |
| 所有指令都没图、只有文字 | 日志搜 `渲染器初始化失败` / `渲染失败`（Pillow 缺失或字体异常） |
| 用户说"点了没反应" | 1.5s 防抖（`command_debounce_ms`）；连点会被静默丢弃，属预期 |
| 偶发"数据库繁忙" | 日志 `数据库抖动`；查是否有事务内 await 慢操作 |
| 内存持续上涨 | `ps` 看 RSS；`/鲸鱼馆` 看缓存条数；确认 `image_cache_size` ≤ 32 |
| 凌晨 4 点重置不对 | 服务器时区：设 `TZ=Asia/Shanghai`（见 README） |
| 概率看起来不对 | 看 `docs/BALANCE.md` 的掉率表 + `POOL_VERSION`；改权重后必须同步文档 |

## 6. 回滚

插件是纯新增目录：停用插件 → 删除目录即可，不影响 AstrBot 本体。
数据在 `data/plugin_data/astrbot_plugin_whale_harem/whale.db`，删库即全服重置（建议先 `.backup`）。