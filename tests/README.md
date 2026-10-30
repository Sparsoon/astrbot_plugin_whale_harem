# tests

本插件的自动化测试放在 `scripts/` 下（不需要 pytest，直接 `python scripts/xxx.py` 跑）：

| 脚本 | 作用 |
|---|---|
| `scripts/smoke_test.py` | 全链路冒烟（签到/喂食/打工/钓鱼/社交/商店/仓库） |
| `scripts/integration_stub_test.py` | 真实 `main.py` + 内嵌 astrbot stub，走 22 条指令 |
| `scripts/test_edge_cases.py` | 惰性跨天重置、保底概率、缓存上限、老库迁移 |
| `scripts/render_demo.py` | 12 张卡片真实出图 |
| `scripts/balance_check.py` | 天气 × 池塘 × 饵料 × 品质的数值模拟 |

全部退出码 0 即为通过。
