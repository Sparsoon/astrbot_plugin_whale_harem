# 2 核 2G 极限防崩溃避坑指南

> 目标机器：2 vCPU / 2 GB RAM / 40 GB SSD，同机跑 **AstrBot + NapCat + 本插件**。
> 这份清单是"血泪版"：每一条都对应一个真实会让进程被 OOM Killer 干掉或事件循环卡死的坑。

---

## 0. 先算清楚预算

| 进程 | 常驻内存（实测区间） | 说明 |
|---|---|---|
| NapCat (Node) | 180 ~ 350 MB | 群多/图多时更高，QQ 客户端内存不可控 |
| AstrBot (Python) | 150 ~ 400 MB | 加载模型 provider、WebUI 后偏高 |
| 本插件 | **+20 ~ 40 MB**（实测渲染进程峰值 **32.2 MB**，含解释器+Pillow） | 5 条 SQLite 连接 + 图片缓存 ≤24 MB |
| 余量 | 约 1.0 ~ 1.4 GB | 留给 Pillow 峰值、系统缓存、突发 |

**结论**：插件本身的预算只有 **60 MB 峰值**。下面所有规则都是为了守住这条线。

```bash
# 上线前先确认 swap（2G 机器没有 swap 等于裸奔）
free -m
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

> **实测对照**：在 Windows + Python 3.12 + Pillow 12.3 上连续渲染 8 张 720px 卡片，
> 进程峰值 WorkingSet 为 **32.2 MB**，单张 63~109 ms；DB 20 并发 P50 11 ms。
> 生产机（阿里云 2C/1.7G，Docker）实测：astrbot 容器 **373.5 MiB**、napcat **255.2 MiB**，
> 上限 1.696 GiB，空闲 CPU 0.3%；容器内单张渲染 **16~26 ms**。
> 即"插件本体 + 一次渲染"的内存预算与本文档估算一致（见 `docs/TEST_REPORT.md`）。

---

## 1. Pillow：90% 的 OOM 都出在这里

### 1.1 绝对不要"先建大图再裁剪"
```python
# ❌ 2000×3000 的图先建出来，光位图就 18 MB，裁剪只省磁盘不省内存
img = Image.new("RGB", (2000, 3000))
...
img = img.crop((0, 0, 720, h))

# ✅ 两次遍历：先测量总高，再按真实高度建图（本项目 ui/renderer.py 的做法）
height = measure_total_height(card)      # 纯计算，不建图
img = Image.new("RGB", (720, height))    # 只建需要的那一张
```

### 1.2 Image / ImageDraw / Font 的生命周期
- `ImageDraw.Draw(img)` 会持有 img 引用，**用完必须 `del draw`**，否则 img 无法释放。
- 不要在 `self.xxx = img`、类属性、闭包、lru_cache 里持有 Image —— 一张 720×1600 的 RGB 是 3.5 MB，十张就是 35 MB。
- 字体对象（`ImageFont.truetype`）**可以**缓存（每个字号一份），它只有几十 KB；但**不要**给每个请求新建字体（解析 ttc 很慢，2 核上会明显卡）。

### 1.3 模式与缩放
- 一律用 `"RGB"`。`"RGBA"` 多 33% 内存，最后还要合成，纯亏。
- 缩放用 `Image.Resampling.LANCZOS` 只用于小图；大图降采样用 `BILINEAR`，LANCZOS 在 2 核上会吃掉 100+ ms CPU。
- 不要在渲染里做 `img.copy()`（双倍内存），直接在同一个 draw 上叠加。

### 1.4 保存与释放
```python
try:
    img.save(path, "PNG", compress_level=6)   # 6 是速度/体积平衡点；9 会明显拖慢 2 核
finally:
    del draw
    del img
    gc.collect()          # 本项目用 collect_now()，2 秒内最多一次，避免频繁全量 GC 抢 CPU
```
- `compress_level=9` + 大图 = 单次 300ms+，群里连发直接卡顿，**别用**。
- 输出图不要超过 **720×1600**。再长就分页或截断，别让用户滑 5000px。

### 1.5 严禁的替代方案
- ❌ Playwright / Chromium / wkhtmltoimage：一个浏览器内核就 200 MB+，2G 机器必死。
- ❌ 每次渲染 `subprocess` 调外部工具：fork+exec 的内存峰值不可控。
- ❌ 逐像素 Python 循环画渐变（720×1600 = 115 万次循环，纯 CPU 空转）。用 `Image.new` + `ImageDraw.line` 按行画，或 `resize` 一张 2×N 的小图拉伸。

---

## 2. 事件循环：卡 1 秒 = 全群消息延迟

### 2.1 同步 IO 黑名单
以下调用**全部**要 `await asyncio.to_thread(...)` 或换异步库：

| 同步调用 | 正确做法 |
|---|---|
| `sqlite3.connect/execute` | `aiosqlite`（本项目连接池 5 条） |
| `open(path).read()` 大文件 | `asyncio.to_thread(Path.read_text)` |
| `requests.get` | `aiohttp` / AstrBot 自带 client |
| `img.save()` / Pillow 全流程 | `await asyncio.to_thread(render_sync)` |
| `time.sleep` | `await asyncio.sleep`（本项目禁止任何 sleep 轮询） |
| 同步 LLM SDK | AstrBot `context.llm_generate`（本身是异步）+ `asyncio.wait_for` 超时 |

### 2.2 每个业务调用都要有超时
```python
await asyncio.wait_for(handler(...), timeout=20)   # 本项目 main.py 统一 20s
```
没有超时 = 一个卡住的 DB 锁或 LLM 请求会把整个指令队列拖死。

### 2.3 LLM 是最大的不确定性
- 必须 `asyncio.wait_for`（本项目 12s）+ 失败降级本地语料；
- 不要在渲染路径里调 LLM；不要并发调多个模型；
- 模型超时/额度耗尽时插件必须**照常出图**。

---

## 3. SQLite：小机器上最容易被忽视的坑

### 3.1 必开 WAL + 合理 PRAGMA
```sql
PRAGMA journal_mode=WAL;      -- 读写并发，读不阻塞写
PRAGMA synchronous=NORMAL;    -- WAL 下安全，比 FULL 快一个量级
PRAGMA busy_timeout=5000;     -- 等锁而不是立刻抛 database is locked
PRAGMA cache_size=-2000;      -- 每连接 2MB，5 条 = 10MB
PRAGMA temp_store=MEMORY;
PRAGMA mmap_size=0;           -- 关 mmap，避免 RSS 虚高被 OOM Killer 盯上
PRAGMA wal_autocheckpoint=200;
```
### 3.2 连接池封顶 5 条
- 每条连接 ≈ 2 MB page cache + 句柄；5 条是"够用且不炸"的甜点。
- 借连接必须 `try/finally` 归还；**永远不要在持有连接时再借第二条**（重入 = 死锁）。
- 本项目用 `asyncio.Queue` 做池，`wait_for(timeout=6)` 拿不到就抛 `DBPoolTimeout`，宁可报错也不无限堆积。

### 3.3 写操作统一 `BEGIN IMMEDIATE`
SQLite 同一时刻只有一个写者。默认的 `BEGIN DEFERRED` 会遇到"读锁升级写锁失败 → SQLITE_BUSY → 重试 → 抖动"。直接 `BEGIN IMMEDIATE` 先把写锁拿走，5 秒 busy_timeout 内排队，**零死锁、零重试**。

### 3.4 JSON 字段不是万能药
- 背包 `items`、`fish` 存 JSON 没问题，但**每次写都是整字段重写**：鱼护 200 条 × 100 字节 = 20 KB/次，一天几千次写入 → WAL 迅速膨胀。
- 对策：鱼护上限 80（配置可调，硬上限 200），超出自动折算鲸币；写入频率靠"一次指令一次事务"压住。
- 千万别在 JSON 里存会长大的数组（如全服日志、每条鱼的完整历史）。

### 3.5 WAL 与磁盘
- WAL 文件会随写入增长，`wal_autocheckpoint=200` 页 ≈ 800 KB 触发一次 checkpoint；进程正常关闭时执行 `wal_checkpoint(TRUNCATE)`。
- 40 GB 盘对 SQLite 绰绰有余，但**不要**把库放在 tmpfs / 内存盘。
- `VACUUM` 会重建整库（2 倍磁盘 + 长时间锁），只在文件 > 48 MB 且低峰期手动做。

### 3.6 每日重置绝对不要用定时器
```python
# ❌ 后台协程轮询：占一个 task、崩溃难发现、休眠/重启就错乱
async def reset_loop():
    while True:
        await asyncio.sleep(60)
        if is_4am(): await reset_all()

# ✅ 惰性比较：读的时候发现 day_key 变了，就地结算
if user.day_key != day_key():      # day_key 以凌晨 4 点为界
    reset_daily_fields(user)
```
本项目所有"每日"状态（签到、喂食次数、娶/离/NTR 保底计数）都是惰性结算，**进程里没有任何定时任务**。

---

## 4. 缓存：不做上限就是慢性自杀

本项目 `ui/cache.py` 三重限制，缺一不可：

| 限制 | 默认 | 不做会怎样 |
|---|---|---|
| 条目数 | 24 | 群里 500 人每人一张 → 几千张图，几十 MB 内存 + 磁盘 |
| 总字节 | 24 MB | 单张 200 KB × 24 = 4.8 MB，看似安全，但 PNG 可能更大 |
| TTL | 300 s | 昨天的签到图还在缓存里，命中率极低还占着位置 |

- 淘汰时**必须 `unlink` 文件**，否则磁盘慢慢被填满（40 GB 也会满）。
- 缓存 key 用卡片内容的 md5（本项目 `Card.cache_key()`），不要用 `id(card)`。
- 缓存目录放 `data/plugin_data/<插件名>/imgcache`，插件目录保持只读。

---

## 5. 并发与死锁

### 5.1 三层刹车
1. **单用户防抖** 1.5s：连点直接静默丢弃（不回复 = 最省资源）。
2. **按 uid 串行锁**：同一用户的"读-改-写"不会互相覆盖（背包、保底计数）。
3. **全局并发闸门**：DB 写、Pillow 渲染、LLM 调用各自有上限；渲染并发默认 1。

### 5.2 典型死锁场景
| 场景 | 后果 | 本项目的做法 |
|---|---|---|
| 事务里再借第二条连接 | 池耗尽后自己等自己 | 所有 repo 方法单连接完成 |
| 先 `get_bag` 再 `save_bag`（两个事务） | 并发丢更新 | 统一 `mutate_bag` 单事务读改写 |
| 池塘"先读后写" | 群友同时捐献丢材料 | `donate_materials` 事务内完成 |
| 渲染线程里 await DB | 线程池 + 事件循环交叉等待 | 渲染只吃 Card 纯数据，不碰 DB |

---

## 6. 概率与数值：最容易被"看起来能跑"骗过去

1. **权重为 0 / 负数**：Alias 表构建时会除零或死循环。本项目 `AliasSampler` 过滤 `w<=0` 并校验总和。
2. **浮点累加误差**：不要用 `prob += step` 反复累加判断保底，直接用**次数**判断（`fail >= pity-1 → 必中`）。
3. **保底写反**：`fail` 是"已失败次数"，第 N 次必中意味着 `fail >= N-1`。签约测试要覆盖第 1 次、第 N-1 次、第 N 次。
4. **双倍扣费**：先扣费再掷骰，异常路径不要让用户"扣了钱没结果"。本项目顺序固定：校验 → 扣费 → 掷骰 → 单事务写结果。
5. **重复领奖**：每日里程碑用 `feed_reward_day` 字段兜底，不能只靠 `feed_count == 5`（第 6 次会再触发）。
6. **概率展示与实现不一致**：文案里的成功率必须和代码同一个公式，否则玩家会算出来打脸。

---

## 7. AstrBot / NapCat 侧调优

```yaml
# AstrBot 侧建议
- 插件只开需要的，别装一堆
- LLM provider 超时设短（10~15s），别让插件排队
- WebUI 不用时关掉（省 50~100 MB）
- 日志级别 INFO，别开 DEBUG（日志本身会占内存）

# NapCat 侧
- 图片/语音缓存目录定期清理（默认会存不少）
- 不要开"自动下载所有群文件"
```

**字体**：Linux 上没装 CJK 字体时 Pillow 会画方块。装 `fonts-noto-cjk`（约 60 MB 磁盘，不占常驻内存）或在配置里指定 `render_font_path`。

---

## 8. 监控与应急

```bash
# 内存 top5
ps aux --sort=-%mem | head -6
# 插件数据库体积 / WAL
ls -lh data/plugin_data/astrbot_plugin_whale_harem/
# 检查是否卡在锁上（有 sqlite3 CLI 时）
sqlite3 whale.db 'PRAGMA wal_checkpoint(PASSIVE);'
```
- 群内 `/鲸鱼馆` 可直接看到连接池、渲染并发、缓存条数、下次重置时间。
- 日志里搜 `指令异常` / `指令超时` / `初始化失败`，出现频率高就说明有慢查询或锁竞争。
- 真崩了：`sqlite3 whale.db 'PRAGMA integrity_check;'`，损坏时用 `.recover`，别直接删库。

---

## 9. 上线前 Checklist

- [ ] 装了 CJK 字体（或配置了 `render_font_path`）
- [ ] `free -m` 有 ≥1 GB 可用 + 2 GB swap
- [ ] `aiosqlite`、`Pillow` 版本满足要求（Pillow ≥ 10）
- [ ] 插件目录只读，数据写在 `data/plugin_data/`
- [ ] `image_cache_size` ≤ 32、`render_concurrency` = 1
- [ ] 群里发 10 条指令，`ps` 看内存没有持续上涨
- [ ] 重启 AstrBot 后签到/喂食/保底计数正确（惰性重置生效）
- [ ] 断网/模型超时情况下，喂食依然能出图（降级语料生效）
- [ ] 私聊触发指令有友好提示，不报错
- [ ] 备份 `whale.db`（`sqlite3 whale.db ".backup backup.db"`）