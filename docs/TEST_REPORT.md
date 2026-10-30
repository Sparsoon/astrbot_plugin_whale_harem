# 实测报告（真机跑通）

> 环境：Windows 10 x64 · Python **3.12.14**（DSH 自带运行时）· Pillow **12.3.0** · aiosqlite **0.22.1**
> 结论：**4 套测试全绿**，12 张卡片真实渲染成功，连接池无泄漏，峰值内存 **32.2 MB**。

> 备注：早先 `pwsh` 工具不可用，是因为 DSH 配置里的 `pwshPath` 指向了不存在的
> `D:\DSH\tools\pwsh7\pwsh.exe`。已改为系统 PowerShell 5.1，本机 Python 3.8 过旧（不支持
> `dataclass(slots=True)`），实际测试统一用 DSH 自带的 Python 3.12 + 阿里云镜像装的 aiosqlite。

---

## 1. 测试矩阵

| # | 脚本 | 覆盖 | 结果 |
|---|---|---|---|
| 0 | `python -m compileall -q -f .` | 全量语法/缩进/导入名 | **PASS**（0 error） |
| 1 | `scripts/smoke_test.py` | 签到/补签/喂食×5/打工冷却/钓鱼×6/背包/卖鱼/池塘捐献升级/娶（保底）/后宫/榜单/离婚/NTR/商店扣费/体验券/钟情卡/面板 | **PASS**（36 项断言全过） |
| 2 | `scripts/integration_stub_test.py` | 用最小 astrbot stub 走真实 `main.py`：21 条指令注册、插件初始化、@ 解析、防抖、群白名单、私聊、业务异常→错误卡、terminate 清理 | **PASS**（26 项） |
| 3 | `scripts/test_edge_cases.py` | 缓存淘汰/同路径不误删/TTL、惰性跨天重置、好感衰减、保底概率、顶层模块加载兼容、渲染缓存命中 | **PASS**（19 项） |
| 4 | `scripts/bench_db.py -n 200` | 5 连接 × 200 并发混合读写、连接泄漏探针 | **PASS**（0 错误，idle=5/5） |
| 5 | `scripts/render_demo.py` | 8 种卡片真实出图 | **PASS**（8/8，0 失败） |
| 6 | `scripts/sim_economy.py --n 200000` | 蒙特卡洛核对经济数值 | **PASS**（每竿 49.93 vs 文档 50.0） |

---

## 2. 性能实测

### 2.1 数据库（2 核目标机型的上界参考）

| 并发 | P50 | P95 | max | 吞吐 | 连接泄漏 |
|---|---|---|---|---|---|
| 20 并发（≈真实群高峰） | **11.0 ms** | 59.3 ms | 81.8 ms | 244 op/s | 无 |
| 200 并发（压力上界） | 147.5 ms | 771.8 ms | 959.2 ms | 207 op/s | 无（idle=5/5） |

> 200 并发全部排队在 SQLite 单写者（`BEGIN IMMEDIATE`）上，这是预期行为；
> 真实场景有 1.5s 单用户防抖，20 并发已是很激进的估计。

### 2.2 渲染（Windows + msyh.ttc + 720px 宽）

| 指标 | 实测 |
|---|---|
| 单张耗时 | 典型 30 ~ 110 ms（Windows 杀软扫描时偶发 ~0.5 s 尖峰；Linux 服务器预期更稳） |
| 12 张连续渲染 | 全部成功，PNG 15 ~ 100 KB/张 |
| **进程峰值 WorkingSet** | **32.2 MB**（含 Python 解释器 + Pillow 导入） |
| 缓存占用（8 张） | 446 KB / 24 MB 上限 |
| 命中缓存 | 命中 1 次，耗时 ≈ 0（同一 Card 第二次渲染返回同路径） |

> 32 MB 峰值意味着在 2G 机器上，插件本体 + 一次渲染的内存预算是安全的
> （NapCat + AstrBot 才是大头，见 `docs/ANTI_CRASH.md` §0）。

### 2.3 经济数值（20 万竿蒙特卡洛）

| 指标 | 模拟值 | 文档值 | 偏差 |
|---|---|---|---|
| 单竿期望（不含材料） | **49.93** | 50.0 | −0.14% |
| 单竿期望（含材料） | **52.14** | 52.2 | −0.1% |
| 鱼 / 道具 / 物品 | 49.9% / 24.9% / 25.2% | 50/25/25 | 正常抖动 |
| 1★~5★（占上钩） | 18.4/14.8/9.6/5.2/1.9 % | 36.8/29.8/19.3/10.4/3.8 %（占鱼获） | ×50% 后一致 |
| 物品品质（小蓝→大红） | 29.5/29.2/20.2/10.6/6.0/2.7/1.5/0.3 % | 29.8/29.1/20.2/10.5/6.1/2.7/1.4/0.25 % | 一致 |

> `docs/BALANCE.md` 的期望表、掉率表、`POOL_VERSION` 与真实采样器完全对得上。

各级池塘实测（每档 6 万竿，普通饵，不含材料）：

| 池塘 | 模拟 | 文档 | 偏差 |
|---|---|---|---|
| Lv1 | 50.21 | 50.0 | +0.4% |
| Lv3 | 62.28 | 61.4 | +1.4% |
| Lv5 | 74.81 | 73.8 | +1.4% |
| Lv10 | 97.66 | 97.0 | +0.7% |
| Lv10 + 特级饵 + 承包券 | 129.56 | 131 | −1.1% |

### 2.4 出图预览

| 卡片 | 预览 |
|---|---|
| 签到 | [checkin.png](preview/checkin.png) |
| 抛竿等待 | [cast.png](preview/cast.png) |
| 收竿（5★） | [fish.png](preview/fish.png) |
| 上工 | [work_start.png](preview/work_start.png) |
| 收工 | [work_claim.png](preview/work_claim.png) |
| 仓库 | [warehouse.png](preview/warehouse.png) |
| 钓鱼（5★） | [fish.png](preview/fish.png) |
| 池塘 | [pond.png](preview/pond.png) |
| 成婚 | [marry.png](preview/marry.png) |
| 商店 | [store.png](preview/store.png) |
| 个人面板 | [profile.png](preview/profile.png) |
| 帮助 | [help.png](preview/help.png) |
| 错误卡 | [error.png](preview/error.png) |

---

## 3. AstrBot 4.29.3 真实源码核对

下载 `astrbot-4.29.3` wheel（仅解包、未安装），逐项核对插件用到的 API：

| 用法 | 源码结论 |
|---|---|
| `@filter.command("签到", alias={...})` | `register_command(command_name, sub_command=None, alias: set \| None = None, **kwargs)` ✅ |
| `class X(Star)` + `@register(...)` | `register_star` 已废弃但仍可用；加载器支持"未加装饰器"的 legacy 路径（类名以 `Plugin` 结尾即可），我们的 `WhaleHaremPlugin` 两条路都能被识别 ✅ |
| `Star.__init__(context, config)` | 4.29 签名为 `(context, config: dict \| None = None)`；4.9 只收 `context`，我们已用 `try/except TypeError` 双兼容 ✅ |
| `initialize()` / `terminate()` | `star_manager` 会 `await star_cls.initialize()` / `.terminate()` ✅ |
| `metadata.yaml` | 必填 `name/desc/version/author`，`astrbot_version` 走 PEP 440 `SpecifierSet`；我们的 `>=4.9.0` 合法 ✅ |
| `_conf_schema.json` | 顶层 `{key: {type, default, ...}}`；支持类型 `int/float/bool/string/text/list/file/object/dict`；我们用 bool/int/string/list 全部合法 ✅ |
| `event.get_sender_name() / get_group_id() / image_result(path)` | 均存在于 `AstrMessageEvent` ✅ |
| `At.qq` / `At.name` | `qq: int \| str`、`name: str \| None`，`qq` 为 `"all"` 时需排除（我们已排除） ✅ |
| `unified_msg_origin` | 是 property（带 setter）✅ |
| 插件模块路径 | 加载为 `data.plugins.<目录名>.main`，相对导入可用；我们额外做了"顶层模块加载"兜底 ✅ |

---

## 4. 修复记录（本轮实测暴露/复核出来的）

| 问题 | 修复 |
|---|---|
| `pwshPath` 指向不存在路径，所有命令执行失败 | 改 DSH `cordis.patch.yml` 指向系统 PowerShell（环境问题，非插件） |
| 错误卡把 hint 同时写进正文与 footer，同句出现两次 | `error_card` 只放正文块 |
| 签到卡的"日历"插画与鱼重叠、像十字架 | 调整 `draw_calendar` 的鱼/日历布局 |
| 整合测试若中途异常会挂住（aiosqlite 线程 non-daemon） | 测试脚本用 `try/finally` 保证 `terminate()`；生产由插件 `terminate()` 正常关闭 |
| `@register` 未来可能被移除导致 ImportError | 兼容导入 + 空装饰器兜底 |
| 4.9 的 `Star.__init__` 不收 config | `super().__init__(context, config)` + `TypeError` 回退 |

---

## 5. 真机 2：阿里云 Docker 生产环境（已部署 v1.1）

### 5.1 v1.1 上线记录（2026-10-25 21:30）

```
[21:30:36] 数据库迁移：users.fish_group 已补列
[21:30:36] 数据库结构升级 1 -> 2
[21:30:36] 鲸鱼馆数据库就绪: /AstrBot/data/plugin_data/.../whale.db (连接数 5, WAL)
[21:30:36] 鲸鱼馆中文字体: /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
[21:30:36] 赛博鲸鱼馆启动完成 | llm=True cache=24 render=1 debounce=1500ms
```

* 磁盘迁移：users 表 7 个新列全部补齐（含 `fish_group`），老数据零丢失；
* AstrBot 自动生成的配置已包含 `fish_wait_min=5 / fish_wait_max=60`；
* 上线过程中踩到/修掉的两个运维坑：
  1. 备份目录不能放在 `data/plugins/` 里，否则会被当成插件加载报错 → 统一放 `/root/astrbot/backups/`；
  2. 新索引依赖 v2 新列，必须"先补列再建索引"，否则旧库启动时 `CREATE INDEX` 先报 `no such column`，
     迁移根本没机会执行（已调整 `_migrate` 顺序，并把索引从建表脚本挪到迁移里）。

| 项 | 实测 |
|---|---|
| 宿主机 | Ubuntu 22.04.5 / 2 vCPU / 1736 MB / 40 GB（39% 已用）/ swap 4 GB |
| 部署形态 | Docker：`astrbot`（soulter/astrbot:latest）+ `napcat`，`/root/astrbot/data` 绑定到容器 `/AstrBot/data` |
| 容器内 Python | 3.12.15，**Pillow 13.0.0**、**aiosqlite 0.22.1** 已自带 |
| 中文字体 | 容器内自带 `NotoSansCJK-Regular.ttc`（无需额外安装） |
| 离线测试 | compileall 0 error；smoke 全过；integration 全过；edge 19 项全过；sim Lv1=**50.15** |
| 渲染 | 8 张全部成功，**单张 16~26 ms**（比 Windows 的 30~110 ms 更快），中文正常 |
| 插件路径 | `/root/astrbot/data/plugins/astrbot_plugin_whale_harem` |
| 数据/配置 | `/AstrBot/data/plugin_data/astrbot_plugin_whale_harem/whale.db`（WAL）、`data/config/astrbot_plugin_whale_harem_config.json` |
| 加载日志 | `Plugin astrbot_plugin_whale_harem (1.0.0) by 煤球` → `鲸鱼馆数据库就绪` → `赛博鲸鱼馆启动完成` |
| 容器内存 | astrbot **373.5 MiB** / napcat 255.2 MiB（限额 1.696 GiB），CPU 空闲 0.3% |

> 结论：**2 核 1.7G 的真实生产环境上，插件加载、建库、出图、WAL 全部正常**；
> 唯一一条 error 来自另一个插件的 OpenAI key 配置，与本插件无关。

### 5.2 真机 3：Windows 大号 → bot 私聊（v1.2 实测，2026-10-26）

测试方式：Windows 端 QQ（大号「不会翻身的有鱼」）私聊 bot `2506059412`（昵称「煤球」），
用 Win32 截图 + SendKeys 自动发送，服务器侧用 XShell 窗口跑 sqlite/日志检查。

| 指令 | 结果 | 关键证据 |
|---|---|---|
| `/帮助` | ✅ 回图 | 2D 立绘版帮助卡 |
| `/签到` | ✅ 回图 | 鲸币 130、连续 2 天、已签到 |
| `/购买 白米饭 3` | ✅ 回图 | 扣 45 鲸币、余额 85、米饭 x3 |
| `/喂食`（不用 @） | ✅ 回图 | 本轮好感 +2、鲸鱼娘好感 2、鲸币 +16 |
| `/仓库` | ✅ 回图 | 4 类 6 件、估值 48 鲸币、鱼护 0 条 |
| `/钓鱼` | ✅ 回图 | 抛竿卡：等待 36 分钟、可咬钩 13:44、饵料 蚯蚓 |
| `/打工`（钓鱼等待中） | ✅ 回图 | **钓鱼未收竿也能上工**：177 分钟、预计 167 鲸币 |
| 发一条普通消息（打工到点） | ✅ 自动回图 | 「收工，领工资！」+167、余额 268 |
| 发一条普通消息（钓鱼到点） | ✅ 自动回图 | 咬钩：蓝鲸鱼 ★★、2395g、74 鲸币、鱼护 1/80 |

> 说明：
> 1. 私聊走 `private:<uid>` 作用域，池塘/钓鱼/打工/喂食都能独立测试，不会污染群数据；
> 2. **钓鱼与打工已解耦**：上工后再抛竿、抛竿后再上工都实测通过；
> 3. **喂食已改为喂 bot**：不 @ 任何人，好感记在 `users.bot_affection`，与婚姻系统无关；
> 4. 代码更新后需要重载一次插件（AstrBot 重启会加载新代码）；测试本身只发消息，不再需要重启 NapCat/QQ。

### 5.3 v1.3 私聊实测（2026-10-26 18:5x）

* `/池塘`：天气块正确显示「流星雨 / 品质+1 / 鱼星+1 / 鲸币×1.3 / 上钩-10% / 下次变化 20:00」。
* `/钓鱼`：抛竿卡显示 基础等待 46 → 天气修正 ×0.90 → 41 分钟，可提杆 19:27。
* 本人发消息提杆：65% 上钩率（Lv1 + 红虫 + 流星雨 = 0.72+0.02+0.01-0.10）实测出卡。
* `/使用 天气控制机 晴天`：成功把本群天气锁定 3 天，卡片显示增益「等待 -15%」。
* 卡片署名：标题下方显示「群友 · 不会翻身的有鱼」。
* 数据库 v3→v4 迁移成功（feed_slots/feed_total/feed_milestone/weather/weather_until）。

### 5.4 v1.4 本地回归（2026-10-28）

* compileall 0 error；smoke（含饭价/打工时长/材料种类/自动购买偏好断言）、integration、edge、render、balance 全部通过。
* 新数值校验：米饭 8 < 蚯蚓 14；打工 60~1440 分钟；池塘材料 6 种。
* 错误卡：`/使用 不存在的东西` 返回可执行错误卡（原因 + 下一步 + 示例），不再是单行提示。

### 5.5 v1.5 花园离线验收（2026-10-30）

* `scripts/test_garden.py`：**133 项全 PASS，退出码 0**（临时 SQLite + 真 Repo/SvcCtx + 可拨时钟）。
* 覆盖：种子概率/品质区间、播种→发芽揭品质、浇水冷却与第 4 次突变归零、施肥 2 次/天且必须消耗、
  三肥收益差异、晒太阳时间窗/每日 1 次/晴雨差异、缺水 2 天与枯萎 5 天、收获果实+区间内种子、
  突变封顶、6 入口返回绿色卡且浇水卡无 blocks。
* 回归补充：枯萎后原地重播不再继承旧播种时间；被动成长吃缺水/缺光惩罚；`/播种 种子` 不再误吞 S 种子。
* 平衡表 `docs/GARDEN_BALANCE.md`：用真实服务跑完整周期（7 种天气等权），E 种子期望果实 5.95 ≥ 售价 4；
  D 种子单轮 7.69（差 0.31）但返还种子后每轮净 +8.78，均不会稳亏。

## 6. 仍未验证

1. **真实 QQ 群里的指令交互**：私聊已全链路验证，群聊仍建议你发一条 `/帮助` 确认 @ 段与图片下发。
2. **多群并发 + LLM 开启**：喂食的 LLM 台词在私聊实测返回（`llm=True`），但高并发下的耗时未压测。
3. **长时间运行**：内存/WAL 的 30 天曲线还没数据，建议一周后 `docker stats` + 看 `whale.db-wal` 大小。
