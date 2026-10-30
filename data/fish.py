"""鱼种总表 —— 5 个星级共 26 种鱼，售价 = base_price × 重量系数(0.85~1.15)。

设计要点：
    * FishDef 全部 frozen+slots，进程里只有这 26 个实例，字典查表零拷贝；
    * FISH_BY_STAR 在 import 时分好桶，抽鱼 = 1 次 random 取下标，O(1)，不建临时列表；
    * 鱼护只落库 g（重量）与 p（成交价），鱼名/星级由本文件按 id 反查，DB 不吃冗余字段。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.rng import choice

FISH_POOL_VERSION = "2026.10.25-r1"
# 与 data/drops.py 同名的池子版本常量，方便 A5 把鱼表与掉落表放在一起对账
POOL_VERSION = FISH_POOL_VERSION

# 重量系数区间：最瘦的鱼 0.85 倍价，最肥的鱼 1.15 倍价（均值 1.0，用于数值测算）
WEIGHT_MIN_FACTOR = 0.85
WEIGHT_MAX_FACTOR = 1.15

# 星级显示
STAR_NAMES = {1: "★", 2: "★★", 3: "★★★", 4: "★★★★", 5: "★★★★★"}
STAR_LABEL = {1: "普通", 2: "优良", 3: "稀有", 4: "史诗", 5: "传说"}
STAR_COLORS = {
    1: "#8C96C8",
    2: "#4D6BFE",
    3: "#8B5CF6",
    4: "#F5B301",
    5: "#EF4444",
}


@dataclass(frozen=True, slots=True)
class FishDef:
    """一条鱼的静态定义。id 一旦上线就不要改，鱼护里存的是它。"""

    id: str
    name: str
    star: int
    min_g: int
    max_g: int
    base_price: int
    desc: str = ""

    @property
    def star_name(self) -> str:
        return STAR_NAMES.get(self.star, "★")

    @property
    def star_label(self) -> str:
        return STAR_LABEL.get(self.star, "普通")

    @property
    def color(self) -> str:
        return STAR_COLORS.get(self.star, "#4D6BFE")

    @property
    def display(self) -> str:
        return f"{self.name}·{self.star_name}"


# ---------------------------------------------------------------- 数值表
# 售价区间严格按需求：1★ 18~28 / 2★ 45~70 / 3★ 95~140 / 4★ 210~300 / 5★ 520~800
_RAW: tuple[FishDef, ...] = (
    # ============================ 1★ 普通（票价 18~28）
    FishDef("bai_tiao", "白条", 1, 20, 110, 20, "水皮上最闲不住的一条，尾巴一甩就是一道银光。"),
    FishDef("ni_qiu", "泥鳅", 1, 25, 90, 22, "滑得像抹了油，抓它得用两只手兜着。"),
    FishDef("xiao_ji_yu", "小鲫鱼", 1, 60, 260, 24, "池塘里的老住户，熬汤能鲜掉眉毛。"),
    FishDef("mai_sui_yu", "麦穗鱼", 1, 15, 70, 26, "小得能一口吞，鳞片却亮得像撒了银粉。"),
    FishDef("pang_pi", "鳑鲏", 1, 12, 55, 27, "会变彩虹色的小家伙，赛博池塘的活体霓虹灯。"),
    FishDef("huang_sang", "黄颡鱼苗", 1, 35, 150, 28, "背鳍带刺，摘钩的时候小心扎手。"),
    # ============================ 2★ 优良（票价 45~70）
    FishDef("li_yu", "野生鲤鱼", 2, 400, 2500, 52, "金红色的尾巴，一看就不是养殖塘里出来的。"),
    FishDef("cao_yu", "草鱼", 2, 600, 3000, 56, "啃了半塘水草，长得跟块砖头似的。"),
    FishDef("luo_fei", "罗非鱼", 2, 300, 1800, 60, "命硬脾气也硬，出了水还在甩尾巴。"),
    FishDef("huang_shan", "黄鳝", 2, 150, 700, 65, "从泥里钻出来的，滑得能自己游回水里。"),
    FishDef("nian_yu", "鲶鱼", 2, 500, 3200, 70, "两根胡子比身体还神气，专挑夜里出来活动。"),
    # ============================ 3★ 稀有（票价 95~140）
    FishDef("gui_yu", "鳜鱼", 3, 500, 2500, 105, "一嘴尖牙的伏击手，值钱就值钱在这身肉上。"),
    FishDef("qiao_zui", "翘嘴鲌", 3, 700, 3500, 115, "嘴翘得能挂住月亮，中钩后能跳出水面。"),
    FishDef("lu_yu", "海鲈鱼", 3, 800, 4000, 125, "从咸水一路游进池塘，身上还带着海腥味。"),
    FishDef("shi_ban", "石斑鱼", 3, 900, 4500, 132, "斑点像二维码，扫一下大概是个「贵」字。"),
    FishDef("qing_yu", "青鱼", 3, 2000, 9000, 140, "池底的清道夫，一嘴能咬碎螺壳。"),
    # ============================ 4★ 史诗（票价 210~300）
    FishDef("man_li", "河鳗", 4, 800, 4000, 220, "夜里贴着池底游，抓它像抓一根会跑的绳子。"),
    FishDef("da_yan_jin_qiang", "大眼金枪鱼", 4, 3000, 15000, 245, "眼睛大得像能看穿你的钱包。"),
    FishDef("ju_gu_she", "巨骨舌鱼", 4, 5000, 25000, 265, "上古活化石，鳞片硬得能当盔甲。"),
    FishDef("zhong_hua_xun", "中华鲟", 4, 8000, 30000, 285, "从长江游来的老资格，见到它算你运气好。"),
    FishDef("dian_man", "电鳗", 4, 3000, 12000, 300, "碰一下手麻半天，赛博池塘的移动电源。"),
    # ============================ 5★ 传说（票价 520~800）
    FishDef("lan_qi_jin_qiang", "蓝鳍金枪鱼", 5, 8000, 40000, 560, "深海里的蓝色闪电，一条顶你半年饵料钱。"),
    FishDef("sai_bo_jing_cub", "赛博鲸鱼幼崽", 5, 12000, 50000, 620, "会跟着你的竿梢哼哼，眼睛是两盏蓝色的灯。"),
    FishDef("shen_hai_long", "深海龙鱼", 5, 2000, 9000, 680, "从两千米下爬上来的牙，连灯光都吓得发抖。"),
    FishDef("huang_dai", "巨型皇带鱼", 5, 15000, 60000, 740, "传说它一翻身，整片海都要换个方向。"),
    FishDef("shen_yuan_zuo_tou", "深渊座头鲸", 5, 30000, 120000, 800, "会唱歌的巨物，中钩那刻整个池塘都在震。"),
)

# ---------------------------------------------------------------- 索引
FISH: dict[str, FishDef] = {fish.id: fish for fish in _RAW}

FISH_BY_STAR: dict[int, list[FishDef]] = {}
for _fish in _RAW:
    FISH_BY_STAR.setdefault(_fish.star, []).append(_fish)

FISH_STARS: tuple[int, ...] = tuple(sorted(FISH_BY_STAR))


# ---------------------------------------------------------------- 查询 / 计算
def stars() -> tuple[int, ...]:
    """全部星级（升序）。"""
    return FISH_STARS


def get_fish(fish_id: str) -> FishDef | None:
    """按 id 取鱼种；鱼护里的旧 id 找不到时返回 None，调用方要能兜住。"""
    return FISH.get(fish_id)


def weight_factor(grams: float, fish_def: FishDef) -> float:
    """重量系数：区间下限 0.85，上限 1.15，线性插值，越界自动夹紧。"""
    low = fish_def.min_g
    high = fish_def.max_g
    if high <= low:
        ratio = 1.0
    else:
        ratio = (float(grams) - float(low)) / float(high - low)
    if ratio < 0.0:
        ratio = 0.0
    elif ratio > 1.0:
        ratio = 1.0
    return WEIGHT_MIN_FACTOR + (WEIGHT_MAX_FACTOR - WEIGHT_MIN_FACTOR) * ratio


def sell_value(fish_def: FishDef, grams: float) -> int:
    """最终售价 = base_price × 重量系数(0.85~1.15)，四舍五入到整数鲸币。"""
    value = fish_def.base_price * weight_factor(grams, fish_def)
    return max(1, int(value + 0.5))


def random_fish(star: int, rng=None) -> FishDef:
    """同星级内等概率抽一条（池子只有 4~6 条，一次取下标，O(1)）。"""
    pool = FISH_BY_STAR.get(int(star))
    if not pool:
        pool = FISH_BY_STAR[FISH_STARS[0]]
    return choice(pool, rng)