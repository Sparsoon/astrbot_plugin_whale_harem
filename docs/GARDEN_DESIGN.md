# 花园 / 植物系统（早期设计稿，已被 GARDEN_SPEC 取代）

> 实现契约以 [GARDEN_SPEC.md](GARDEN_SPEC.md) 为准；本文仅保留最早的设计讨论。

> 状态：**未开放**。本文件只是设计契约，代码骨架在 `data/plants.py` 与 `services/garden.py`，
> 没有注册任何指令、没有建表、不会影响现有玩法。

## 1. 定位

钓鱼是"随机产出"，打工是"时间换钱"，花园补上"**长线养成 + 每日照料**"这一环：

- 低操作频率：种下去、偶尔浇水、到点收；
- 和现有系统咬合：种子从钓鱼掉、收成能喂鲸鱼娘、天气影响生长；
- 仍然零轮询：全部用"种植时间戳 + 成熟时间戳"惰性计算。

## 2. 数据契约（规划）

```sql
CREATE TABLE gardens (
    group_id   TEXT PRIMARY KEY,
    slots      TEXT NOT NULL DEFAULT '[]',   -- [{plant, planted_ts, ready_ts, watered_day}]
    updated_at TEXT NOT NULL DEFAULT ''
);
```

不新建 users 字段；种子、果实都走现有 `items` 表（新增 `CAT_SEED` / `CAT_CROP` 类别）。

## 3. 玩法草案

| 项 | 草案 |
|---|---|
| 花盆 | 每群 4 个，Lv.1 解锁 2 个，池塘每 3 级 +1 |
| 生长 | 30 分钟 ~ 12 小时；受天气 `grow_mult` 影响（晴 -10%，雨 +15%） |
| 浇水 | 每天 1 次/盆，缩短 10% 剩余时间（惰性：只改 ready_ts） |
| 收成 | 1~3 个果实，品质受池塘等级与天气影响 |
| 用途 | 果实可喂鲸鱼娘（+好感）、可卖、可做池塘升级材料 |

## 4. 指令（规划，不注册）

- `/花园` —— 看 4 个花盆状态与剩余时间
- `/种植 <种子> [1-4]` —— 种下
- `/浇水 [1-4]` —— 每日一次
- `/收获` —— 收成熟作物

## 5. 实现清单（正式开发时）

1. `data/plants.py` 填 `PLANTS`（6~8 种，对应 3 档品质）；
2. `data/items.py` 加种子/果实，掉落池按池塘等级挂种子；
3. `core/weather.py` 加 `grow_mult` 字段；
4. `services/garden.py` 实现 4 个函数，全部 `repo` 事务；
5. `main.py` 注册 4 条指令 + 帮助文案；
6. `docs/BALANCE.md` 补产出曲线。

## 6. 兼容性

- 不建表也不影响老库；正式上线时走 `_MIGRATIONS` 加 `gardens` 表（v6）；
- 花园功能默认关闭（配置 `garden_enabled=false`），避免误触。
