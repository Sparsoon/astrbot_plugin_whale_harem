# 花园（养植物）系统 · 实现规范 v1

> 本文是交给子智能体的**唯一契约**。实现必须严格按本文件的接口、ID、数值与验收标准；
> 与本文冲突时以本文为准，本文没写的才允许自行决定并在代码注释里说明理由。

## 0. 文件所有权（禁止越界改别人的文件）

| 角色 | 可写文件 | 禁止修改 |
|---|---|---|
| 数据 Agent | `data/plants.py`、`data/garden_items.py` | 其他所有文件 |
| 花园 Agent | `services/garden.py`、`ui/garden.py` | 其他所有文件 |
| 测试 Agent | `scripts/test_garden.py`、`docs/GARDEN_BALANCE.md` | 其他所有文件 |
| 审查 Agent | 只读，输出报告给主控 | 所有文件 |

主控（我）负责：`data/items.py` 挂钩、`core/`、`main.py`、钓鱼/商店/喂食接入、最终联调。

## 1. 品质与种子

### 1.1 植物品质（从低到高）
`小蓝 < 大蓝 < 小紫 < 大紫 < 小红 < 大红 < 星彩`

### 1.2 种子等级（从高到低）
`S > A > B > C > D > E`

### 1.3 种子 → 植物概率（百分比，必须合计 100）

| 种子 | 星彩 | 大红 | 小红 | 大紫 | 小紫 | 大蓝 | 小蓝 |
|---|---|---|---|---|---|---|---|
| S | 5 | 25 | 70 | - | - | - | - |
| A | - | 10 | 25 | 35 | 30 | - | - |
| B | - | 0.5 | - | 25 | 47.5 | 25 | 2 |
| C | - | - | 0.3 | 20 | 79.7 | - | - |
| D | - | - | - | 3 | 32 | 45 | 20 |
| E | - | - | - | - | 5 | 55 | 40 |

规则：
* **C 级种子必出小紫及以上**（上表 C 只有大紫/小紫，即保底）；
* 星彩只能由 S 级种子种出；
* B 出大红概率必须极低（实现取 0.5%，不要超过 1%）；
* C 出小红概率极低（实现取 0.3%），且 C 最低仍保底小紫；

### 1.4 植物 → 可收获种子区间

| 植物品质 | 种子区间（最好~最差） |
|---|---|
| 星彩 | S~S（只出 S） |
| 大红 | S~A |
| 小红 | S~B |
| 大紫 | S~C |
| 小紫 | A~C |
| 大蓝 | B~E |
| 小蓝 | B~E |

突变值决定落在区间内的哪一档：突变越高越靠前（好）；达到该植物上限后不再增长。

## 2. 数据层接口（数据 Agent 必须实现）

### 2.1 `data/plants.py`

保留现有 `PlantDef` 字段并实现：

```python
PLANTS: dict[str, PlantDef]              # id -> 定义
SEED_GRADE_WEIGHTS: dict[str, dict[str, float]]   # 见 1.3，权重用百分比数字即可
PLANT_SEED_RANGE: dict[str, tuple[str, str]]      # 植物品质 -> (best, worst)，见 1.4

def get_plant(plant_id: str) -> PlantDef | None
def all_plants() -> tuple[PlantDef, ...]
def plants_by_grade(grade: str) -> tuple[PlantDef, ...]
def roll_plant_for_seed(seed_grade: str, rng=None) -> PlantDef
def roll_seed_for_plant(plant_grade: str, mutation: int, rng=None) -> str
def seed_grades_for_plant(plant_grade: str) -> tuple[str, ...]
```

* `roll_plant_for_seed` 用 `SEED_GRADE_WEIGHTS` 加权；`rng` 为 `random.Random` 或 None；
* `roll_seed_for_plant`：把 mutation(0~100) 映射到区间位置，0 → worst，100 → best；
  区间是离散档位，按顺序线性插值取档（例如 S~C 共 4 档：0→C，34→B，67→A，100→S）。

### 2.2 植物目录（至少这些，可加但不许少）

每个品质 6~8 种，必须包含用户点名的：

* 星彩：天山雪莲、七色花、不死草、彼岸花（再加 2 种）
* 大红：昙花、兜兰、龟甲牡丹、杜鹃红山茶、珙桐、柏山祖冷杉（保护植物，desc 写科普）
* 小红：牡丹、杜鹃、玫瑰、茉莉、金桂、月季、蔷薇、紫罗兰、薰衣草
* 大紫：向日葵、橘子、橙子、油菜、西瓜、蜜瓜、南瓜、香蕉、茭白、葡萄、菱角、山竹、火龙果、榴莲、荔枝、甘蔗
* 小紫：水稻、玉米、地瓜、花生、大豆、绿豆、豌豆、黑米、红豆
* 大蓝：青菜、黄瓜、莴苣、白菜、菠菜、萝卜、胡萝卜、大葱、小葱、韭菜、芹菜、洋葱、西红柿、西葫芦
* 小蓝：狗尾巴草、车前草、荠菜、马齿苋、鬼针草、龙葵、蒲公英、荨麻、牧草

`id` 用英文小写+下划线（如 `peony`、`rice`）；`fruit_item` 必须是 `crop_<plant_id>`；
`grow_minutes` 按品质递增：小蓝 120、大蓝 180、小紫 300、大紫 420、小红 600、大红 900、星彩 1440（分钟，基准）；
花朵类 `is_flower=True`，果实类 False。

### 2.3 `data/garden_items.py`

用 `ItemDef` 定义并导出 `GARDEN_ITEM_DEFS: tuple[ItemDef, ...]`，必须包含：

| id | 名称 | 品质 | 类别 | 价格 | 说明 |
|---|---|---|---|---|---|
| `seed_s` | S 级种子 | 星彩 | CAT_SEED | 0 | 不可购买 |
| `seed_a` | A 级种子 | 大红 | CAT_SEED | 0 | 不可购买 |
| `seed_b` | B 级种子 | 小红 | CAT_SEED | 0 | 不可购买 |
| `seed_c` | C 级种子 | 大紫 | CAT_SEED | 0 | 不可购买 |
| `seed_d` | D 级种子 | 小紫 | CAT_SEED | 8 | 可购买 |
| `seed_e` | E 级种子 | 小蓝 | CAT_SEED | 4 | 可购买 |
| `fert_n` | 氮肥 | 小蓝 | CAT_FERT | 4 | 成长多、突变少 |
| `fert_p` | 磷肥 | 小紫 | CAT_FERT | 6 | 突变多、成长少 |
| `fert_k` | 钾肥 | 大蓝 | CAT_FERT | 5 | 均衡 |
| `bowl_plain` | 空碗 | 小蓝 | CAT_MEAL | 2 | 装果实喂鱼 |
| `pot_plain` | 空盆 | 大蓝 | CAT_MEAL | 6 | 大份果实餐 |
| `crop_<plant_id>` | 植物名（果实/花） | 对应品质 | CAT_CROP | 0 | 可卖出，sell 按品质：小蓝 4 / 大蓝 7 / 小紫 10 / 大紫 18 / 小红 40 / 大红 120 / 星彩 500 |

所有 `crop_*` 的 `sell` 不能为 0；`usable=False`（收获后卖/做果实餐由服务层处理）。

## 3. 花园服务接口（花园 Agent）

### 3.1 六个入口（固定签名）

```python
async def do_garden(ctx, uid, name, group_id) -> Card
async def do_sow(ctx, uid, name, group_id, arg: str = "") -> Card
async def do_water(ctx, uid, name, group_id) -> Card
async def do_fertilize(ctx, uid, name, group_id, arg: str = "") -> Card
async def do_sun(ctx, uid, name, group_id) -> Card
async def do_harvest(ctx, uid, name, group_id) -> Card
```

`ctx.repo` 已有：`get_garden(group_id, uid)`、`save_garden(garden)`、`delete_garden(...)`；
`Garden` 模型字段见 `core/models.py`。所有写操作必须 `await repo.save_garden(...)`。

### 3.2 数值（必须照做，别自己改成"没用"）

* 成长值 0~600，6 阶段各 100：播种/发芽/幼苗/营养生长/开花/结果；
* 播种后到发芽（growth≥100）才公开植物品质，之前面板显示"???（发芽后揭晓）"；
* **被动成长**：每小时 +2 成长（惰性按整小时结算，最多补 72 小时，防止离线暴涨）；
* **品种速度**：上述动作/被动收益统一乘以品种速度 `clamp(sqrt(300/grow_minutes), 0.45, 1.6) × 种子档位系数（C 1.15 / D 1.30 / E 1.45，S/A/B 1.0）`，
  常见作物更快、星彩更慢（这是"隐式调整成长需求"的实现方式）；
* **浇水**：每 1 小时 1 次；+8 成长、+2 突变；当天第 4 次起突变 +0（成长仍 +8），面板必须窄（只有标题+一行结果）；
* **施肥**：每天 2 次，**必须消耗对应肥料**（没有就报错并提示去 /商店）；
  氮肥 +20 成长/+1 突变，磷肥 +8/+5，钾肥 +12/+3；同日第 3 次报错；
* **晒太阳**：每天 1 次，本地时间 7:00~19:00（可配置）；按天气：
  晴 +30 成长/+8 突变；多云 +24/+6；雨 +14/+3；雷雨 +8/-2；雾 +18/+5；流星雨 +42/+12；寒潮 +10/+1；
  同时记录"最后一次晒太阳"，用于缺光状态；
* **状态**（面板展示）：
  - 缺水：≥2 天没浇水（成长增益 -50%）；≥5 天没浇水 → 枯萎，只能重新播种；
  - 缺氮/缺磷/缺钾：对应肥料 ≥3 天没施（成长 -15% 每种，最多 -30%）；
  - 缺光：≥2 天没晒太阳（成长 -30%）；
  - 状态会影响**后续动作与被动成长的收益**，不能把已经获得的成长扣掉（避免玩家白干）；
* **突变值**：0~100，达到该植物品质的种子上限档位后不再增长（例如小紫上限 A，达到映射值即封顶）；
  浇水/施肥/晒太阳都加突变，保证三者都有用；
* **收获**：growth≥600 才可收；产出 1 个 `fruit_item` + 1 颗种子（按 `roll_seed_for_plant`）；
  收获后清空花园（可重新播种）；枯萎时 `/收获` 返回 0 并清空。

### 3.3 防"机制没用"的硬性检查（验收会测）

* 同一天：浇水 1 次 vs 3 次，成长值后者必须更高（+16 以上），突变更高；
* 施肥 2 次必须比不施肥成长更高；三种肥料对 (成长, 突变) 的侧重必须不同；
* 晒太阳必须同时有天气差异（晴 vs 雨结果不同）；
* 连续 3 天不浇水必须进入缺水并降低成长收益；5 天必须枯萎；
* 突变封顶后继续浇水，突变不得再涨（成长仍涨）。

### 3.4 UI（`ui/garden.py`）

* 所有花园卡片 `accent = C.GARDEN_ACCENT`（绿色），tone 可用 success/normal；
* `do_garden` 输出：帮助（6 条指令）+ 总体状态（阶段、成长/600、突变、状态、天气）；
* `do_water` 面板必须**窄**：一张小卡，标题+一行"成长 +8 / 突变 +2 / 今日第 N 次"，不加大段说明；
* `do_sow` 不带参数时列出背包种子（名称、数量、可播种）并提示 `/播种 <种子名>`；
* 面板文案中文、简洁，不出现内部字段名。

## 4. 测试 Agent 验收清单

`scripts/test_garden.py` 必须离线跑（临时 DB + 假 SvcCtx，参考 smoke_test.py）：

1. 播种 → 阶段=播种期，品质未公开；
2. 加成长到 100 → 发芽期，品质公开；
3. 浇水冷却（1 小时内第二次报错）、第 4 次突变不涨；
4. 施肥每日 2 次上限 + 三种肥料收益差异；
5. 晒太阳时间窗（可用 monkeypatch 时间）、每日 1 次、天气差异；
6. 缺水 2 天/枯萎 5 天（构造时间戳）；
7. 收获：growth≥600 才可收，得到果实+种子，种子档位在植物区间内；
8. 突变封顶后不再增长；
9. 6 个入口都返回 Card，且浇水卡明显更矮（blocks 为空）。

## 5. 数据平衡要求（测试 Agent 出 `docs/GARDEN_BALANCE.md`）

* D/E 种子价格与产出：买 E 种子（4）种小蓝/大蓝，期望果实卖出 ≥ 4，不能稳亏；
* S 种子不可购买，只能靠钓鱼极低概率或高级植物回收；
* 计算 7 个品质各自的期望周期（分钟）与期望收益，列成表；
* 说明"突变 → 种子档位"的映射，并给出各品质的保底/上限。

## 6. 禁止事项

* 不许加定时器/轮询（`asyncio.sleep` 循环、APScheduler 等）；
* 不许在 service 里 import PIL；
* 不许改 `main.py`、`core/`、`data/items.py`（这些由主控接入）；
* 不许把数值硬编码到 UI 里，数值常量集中在 `services/garden.py` 顶部或 `core/constants.py`（只能读不能改）。
