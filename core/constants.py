"""赛博鲸鱼馆 —— 公共常量与数值开关。

所有"魔法数字"集中在此，方便平衡调整。硬性规则：
    * 每日重置只做惰性计算（compare day_key），绝不注册定时器。
    * 任何概率/概率保底都由此处的常量驱动。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 时间
RESET_HOUR = 4  # 每日凌晨 4 点重置
DAY_SECONDS = 86400

# ---------------------------------------------------------------- 通用
PLUGIN_NAME = "astrbot_plugin_whale_harem"
MAX_GROUP_TEXT_LEN = 64

# ---------------------------------------------------------------- 品质
RARITY_XIAOLAN = "小蓝"
RARITY_DALAN = "大蓝"
RARITY_XIAOZI = "小紫"
RARITY_DAZI = "大紫"
RARITY_XIAOJIN = "小金"
RARITY_DAJIN = "大金"
RARITY_XIAOHONG = "小红"
RARITY_DAHONG = "大红"
RARITY_STAR = "星彩"   # 花园专属：传说植物

RARITY_ORDER = [
    RARITY_XIAOLAN,
    RARITY_DALAN,
    RARITY_XIAOZI,
    RARITY_DAZI,
    RARITY_XIAOJIN,
    RARITY_DAJIN,
    RARITY_XIAOHONG,
    RARITY_DAHONG,
    RARITY_STAR,
]

RARITY_RANK = {name: idx for idx, name in enumerate(RARITY_ORDER)}

# UI 主色（DeepSeek 蓝系 + 品质色）
RARITY_COLORS = {
    RARITY_XIAOLAN: "#4D6BFE",
    RARITY_DALAN: "#2F4BD8",
    RARITY_XIAOZI: "#8B5CF6",
    RARITY_DAZI: "#6D28D9",
    RARITY_XIAOJIN: "#F5B301",
    RARITY_DAJIN: "#D98324",
    RARITY_XIAOHONG: "#EF4444",
    RARITY_DAHONG: "#B91C1C",
    RARITY_STAR: "#7DE2D1",
}

# ---------------------------------------------------------------- 品牌
BRAND_NAME = "赛博鲸鱼馆"
BRAND_FISH_NAME = "深蓝大肥鱼"
ACCENT_BLUE = "#4D6BFE"
DEEP_BLUE = "#0B1330"
PANEL_BLUE = "#141C3A"
TEXT_MAIN = "#E8ECFF"
TEXT_DIM = "#8C96C8"

# ---------------------------------------------------------------- 池塘
POND_MAX_LEVEL = 10
POND_BASE_LEVEL = 1
# 每级池塘对"上钩率"的加成（绝对值，封顶）
POND_HOOK_BONUS_PER_LEVEL = 0.02
POND_HOOK_BONUS_CAP = 0.15

# 池塘升级所需材料（等级 -> {材料ID: 数量}）
POND_UPGRADE_REQUIREMENTS = {
    2: {"mat_stone": 10},
    3: {"mat_stone": 16, "mat_shell": 4},
    4: {"mat_shell": 10, "mat_coral": 8},
    5: {"mat_coral": 14, "mat_stone": 12, "mat_shell": 6},
    6: {"mat_bone": 12, "mat_amber": 2, "mat_coral": 6},
    7: {"mat_bone": 18, "mat_amber": 5, "mat_shell": 8},
    8: {"mat_bone": 24, "mat_amber": 8, "mat_crystal": 3},
    9: {"mat_crystal": 8, "mat_amber": 10, "mat_bone": 12},
    10: {"mat_crystal": 14, "mat_amber": 12, "mat_coral": 10},
}

# 材料掉落：每次成功上钩必定额外产出（"无视道具概率必出，量随机"）
MATERIAL_DROP_MIN = 1
MATERIAL_DROP_MAX = 2

# ---------------------------------------------------------------- 钓鱼
HOOK_BASE_RATE = 0.72          # 1 级池塘基础上钩率
HOOK_RATE_FLOOR = 0.45
HOOK_RATE_CEIL = 0.96
TICKET_HOOK_BONUS = 0.05       # 水库体验券
PRIVATE_POOL_HOOK_BONUS = 0.18 # 私人鱼塘体验券

# 上钩后大类比例：鱼 50% / 道具 25% / 物品 25%
CATEGORY_WEIGHTS = {"fish": 50, "prop": 25, "item": 25}

# 池塘等级带来的鱼获品质提升（每级给星级权重的小幅上移）
POND_STAR_BONUS_PER_LEVEL = 0.08
POND_STAR_BONUS_CAP = 0.6

# 钓鱼等待：抛竿后要等 5~60 分钟，天气会改写倍率
FISH_WAIT_MIN = 5
FISH_WAIT_MAX = 60
# 到点后，**抛竿本人再发一条消息**才提杆；每次消息 65% 触发（没中就再发）
LIFT_CHANCE = 0.65
# 体验券池塘：按固定 Lv.5 计算上钩率/品质，但**不受实际池塘品质影响**（天气仍生效）
TICKET_POND_LEVEL = 5
# 天气控制机持续时长（3 天）
WEATHER_CONTROL_SECONDS = 3 * 86400

# 婚姻关系的"恩爱值"每日自然增长（喂食已改为喂 bot，不再刷群友好感）
HAREM_AFFECTION_PER_DAY = 2

# 鱼护容量兜底（配置文件可覆盖）
DEFAULT_FISH_BAG_LIMIT = 80
FISH_BAG_HARD_LIMIT = 200

# ---------------------------------------------------------------- 签到（长线）
CHECKIN_BASE_COINS = 50
CHECKIN_MAX_STREAK_DAYS = 20     # 第 20 天达到满额
CHECKIN_MAX_COINS = 100          # 20 天封顶 100 鲸币/天
CHECKIN_BAIT_MIN = 1
CHECKIN_BAIT_MAX = 3
CHECKIN_RICE_CHANCE = 0.35     # 签到时额外掉一碗白米饭的概率

# ---------------------------------------------------------------- 喂食（早/中/晚 3 次）
FEED_AFFECTION_MIN = 1
FEED_AFFECTION_MAX = 3
FEED_COIN_MIN = 8
FEED_COIN_MAX = 24
FEED_DAILY_LIMIT = 3             # 每天最多 3 次（早/中/晚各一次）
FEED_SLOTS = ("早餐", "午餐", "晚餐")
# 各时段（小时，左闭右开）：早餐 5-10、午餐 10-16、晚餐 16-22；其余时间"不是饭点"
FEED_SLOT_HOURS = ((5, 10), (10, 16), (16, 22))
# 累计投喂里程碑（一次性奖励，间隔逐渐拉大）
FEED_TOTAL_MILESTONES = (10, 20, 40, 80, 160)
FEED_MILESTONE_REWARDS = {
    10: {"coins": 60, "items": {"bait_normal": 2}},
    20: {"coins": 150, "items": {"bait_worm": 2}},
    40: {"coins": 300, "items": {"bait_bloodworm": 2}},
    80: {"coins": 600, "items": {"bait_corn": 2}},
    160: {"coins": 1200, "items": {"ticket_star": 1}},
}
FEED_AFFECTION_MARRY_BONUS_PER_POINT = 0.005
FEED_AFFECTION_MARRY_BONUS_CAP = 0.15

# ---------------------------------------------------------------- 打工
WORK_MIN_MINUTES = 60
WORK_MAX_MINUTES = 1440
WORK_COIN_PER_MINUTE = 0.35    # 每分钟基础鲸币（1 小时约 21，24 小时约 504）
WORK_RANDOM_SPREAD = 0.25      # ±25% 随机浮动
WORK_BONUS_CHANCE = 0.12       # 摸鱼暴击概率
WORK_BONUS_RATE = 1.8          # 暴击倍率
WORK_ENERGY_COST = 0           # 预留字段：体力（当前不做体力系统）

# ---------------------------------------------------------------- 婚姻
MARRY_COST = 100
MARRY_BASE_RATE = 0.10
MARRY_STEP_RATE = 0.10
MARRY_PITY = 5                 # 第 5 次必中

DIVORCE_COST = 100
DIVORCE_BASE_RATE = 0.50
DIVORCE_PITY = 2               # 第 2 次必中

NTR_COST = 125
NTR_BASE_RATE = 0.05
NTR_STEP_RATE = 0.05
NTR_PITY = 10                  # 第 10 次必中

HAREM_DISPLAY_LIMIT = 10
LOYAL_CARD_DURATION = 24 * 3600  # 钟情卡保护时长
PURSUIT_DECAY_PER_DAY = 0.5      # 好感度每日自然衰减（惰性结算）

# ---------------------------------------------------------------- 背包
BAG_DEFAULT_LIMIT = 9999
ITEM_STACK_LIMIT = 9999

# ---------------------------------------------------------------- 花园 / 植物
GARDEN_ACCENT = "#2FBF71"        # 花园统一绿色
GARDEN_STAGE_SECONDS = 3600      # 惰性结算粒度（1 小时）
GARDEN_STAGES = ("播种期", "发芽期", "幼苗期", "营养生长期", "开花期", "结果期")
GARDEN_STAGE_CAP = 100           # 每个阶段 100 成长值
GARDEN_GROWTH_MAX = len(GARDEN_STAGES) * GARDEN_STAGE_CAP   # 600
GARDEN_MUTATION_CAP = 100        # 突变值上限（百分比刻度）
GARDEN_SEED_GRADES = ("S", "A", "B", "C", "D", "E")
GARDEN_PLANT_GRADES = (
    "小蓝", "大蓝", "小紫", "大紫", "小红", "大红", "星彩",
)
GARDEN_SUN_START_HOUR = 7
GARDEN_SUN_END_HOUR = 19
GARDEN_TIMEZONE = "Asia/Shanghai"
