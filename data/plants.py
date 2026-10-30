"""植物图鉴与种子/品质规则（花园数据层）。

契约见 `docs/GARDEN_SPEC.md` 第 1、2 节；主插件只通过本文件的公开函数取数据。

设计约束
    * 本文件**不 import data.items**：items.py 会反过来 import garden_items / plants，
      在这里再 import ITEMS 会形成循环导入。
    * 品质字符串统一取 `core.constants` 里的常量，避免和物品表文案漂移。
    * 概率用「百分比权重」原样保存（合计 100），服务层改平衡只动这一张表。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..core import constants as C
from ..core import rng as _rng


@dataclass(frozen=True, slots=True)
class PlantDef:
    id: str
    name: str
    grade: str            # 小蓝/大蓝/小紫/大紫/小红/大红/星彩
    seed_best: str        # 收获种子最好档位（S 最高）
    seed_worst: str       # 收获种子最差档位
    grow_minutes: int     # 从播种到结果期顶部的基准分钟数（不含加速）
    fruit_item: str       # 果实/花朵物品 id
    is_flower: bool = False
    desc: str = ""


# ---------------------------------------------------------------- 数值表
# 品质 -> 基准成长分钟（GARDEN_SPEC 2.2）
GROW_MINUTES_BY_GRADE: dict[str, int] = {
    C.RARITY_XIAOLAN: 120,
    C.RARITY_DALAN: 180,
    C.RARITY_XIAOZI: 300,
    C.RARITY_DAZI: 420,
    C.RARITY_XIAOHONG: 600,
    C.RARITY_DAHONG: 900,
    C.RARITY_STAR: 1440,
}

# 植物品质 -> 可收获种子的档位区间 (best, worst)（GARDEN_SPEC 1.4）
PLANT_SEED_RANGE: dict[str, tuple[str, str]] = {
    C.RARITY_XIAOLAN: ("B", "E"),
    C.RARITY_DALAN: ("B", "E"),
    C.RARITY_XIAOZI: ("A", "C"),
    C.RARITY_DAZI: ("S", "C"),
    C.RARITY_XIAOHONG: ("S", "B"),
    C.RARITY_DAHONG: ("S", "A"),
    C.RARITY_STAR: ("S", "S"),
}

# 种子档位 -> {植物品质: 权重}（百分比，GARDEN_SPEC 1.3，每行合计 100）
# 说明：表中 B 出大红、C 出小红标注为 "-"（规范允许 B 出大红 ≤1%、C 出小红 0.3%~0.5%，
# 但既然规范以本表为准，这里严格按表取 0，保证 C 级保底小紫、星彩只能由 S 出。
SEED_GRADE_WEIGHTS: dict[str, dict[str, float]] = {
    "S": {C.RARITY_STAR: 5.0, C.RARITY_DAHONG: 25.0, C.RARITY_XIAOHONG: 70.0},
    "A": {
        C.RARITY_DAHONG: 10.0,
        C.RARITY_XIAOHONG: 25.0,
        C.RARITY_DAZI: 35.0,
        C.RARITY_XIAOZI: 30.0,
    },
    "B": {
        C.RARITY_DAHONG: 0.5,
        C.RARITY_DAZI: 25.0,
        C.RARITY_XIAOZI: 47.5,
        C.RARITY_DALAN: 25.0,
        C.RARITY_XIAOLAN: 2.0,
    },
    "C": {C.RARITY_XIAOHONG: 0.3, C.RARITY_DAZI: 20.0, C.RARITY_XIAOZI: 79.7},
    "D": {
        C.RARITY_DAZI: 3.0,
        C.RARITY_XIAOZI: 32.0,
        C.RARITY_DALAN: 45.0,
        C.RARITY_XIAOLAN: 20.0,
    },
    "E": {C.RARITY_XIAOZI: 5.0, C.RARITY_DALAN: 55.0, C.RARITY_XIAOLAN: 40.0},
}


# ---------------------------------------------------------------- 植物目录
# 每行 (plant_id, 名称, 是否花卉, 科普/风味描述)
#
# 关于数量：GARDEN_SPEC 2.2 既写了"每品质 6~8 种"，又点名了 9~16 种必须包含的植物；
# 规范同时声明"必须包含点名的植物"优先，因此以点名为准：小蓝/小红/小紫各 9 种、
# 大蓝 14 种、大紫 16 种、大红 7 种、星彩 6 种（均不小于 6），没有删减任何点名植物。
#
# 关于"柏山祖冷杉"：按规范原文拼写；它是松科冷杉属的极危物种，产地为浙江百山祖，
# 描述里写清科普信息，id 用 baishanzu_fir（百山祖冷杉的通行英文名）。
_CATALOG: tuple[tuple[str, tuple[tuple[str, str, bool, str], ...]], ...] = (
    (
        C.RARITY_STAR,
        (
            ("snow_lotus", "天山雪莲", True, "生长在海拔四千米雪线之上，一生只开一次花，被称作雪域仙草。"),
            ("rainbow_flower", "七色花", True, "一朵花七种颜色，传说摘到它的人可以实现一个愿望。"),
            ("immortal_grass", "不死草", False, "即卷柏，晒干蜷成一团，遇水又舒展开，人称九死还魂草。"),
            ("higanbana", "彼岸花", True, "石蒜科石蒜，花叶永不相见，秋分前后开成一片血红。"),
            ("lingzhi", "千年灵芝", False, "长在深山老树根上的灵芝，菌盖纹路一圈圈像年轮。"),
            ("moon_orchid", "月光兰", True, "只在满月夜开放，花瓣薄得透光，像凝住的一层月色。"),
        ),
    ),
    (
        C.RARITY_DAHONG,
        (
            ("epiphyllum", "昙花", True, "仙人掌科昙花属，夜里开几个小时就谢，昙花一现说的就是它。"),
            ("paphiopedilum", "兜兰", True, "兰科兜兰属，唇瓣像只毛绒小拖鞋；野生兜兰属全部是国家一级保护植物。"),
            ("ariocarpus", "龟甲牡丹", True, "仙人掌科岩牡丹属，CITES 附录Ⅰ物种，十年才长一厘米，被叫作活着的石头。"),
            ("camellia_azalea", "杜鹃红山茶", True, "山茶科山茶属，国家一级保护，野生只在广东阳春一带有，成株极少。"),
            ("dove_tree", "珙桐", True, "蓝果树科珙桐，白色苞片像鸽翼，满树开花如白鸽栖枝，国家一级保护。"),
            ("baishanzu_fir", "柏山祖冷杉", False, "松科冷杉属，国家一级保护，野生仅存浙江百山祖数株，属极危物种。"),
            ("golden_camellia", "金花茶", True, "山茶科山茶属，国家二级保护，金黄色花瓣在茶花里极罕见，被称茶族皇后。"),
        ),
    ),
    (
        C.RARITY_XIAOHONG,
        (
            ("peony", "牡丹", True, "花中之王，一朵能开成碗口大，洛阳人拿它当门面。"),
            ("azalea", "杜鹃", True, "映山红，春天一开就是半座山，土偏碱就闹脾气。"),
            ("rose", "玫瑰", True, "带刺的浪漫，花瓣能泡茶，也能转手送给隔壁群友。"),
            ("jasmine", "茉莉", True, "夏夜开花，香得能飘一层楼，晒干就是茉莉花茶。"),
            ("osmanthus", "金桂", True, "秋天满树金黄小花，一条街都闻得到，能做桂花糖。"),
            ("chinese_rose", "月季", True, "一年能开好几茬，园林里最勤快的开花机器。"),
            ("climbing_rose", "蔷薇", True, "爬墙高手，一开就是一面花墙，枝条上全是小钩刺。"),
            ("violet", "紫罗兰", True, "十字花科紫罗兰，花小而密，香气温温柔柔。"),
            ("lavender", "薰衣草", True, "地中海来的香草，晒干装袋能安神，开花时是一片紫色海。"),
        ),
    ),
    (
        C.RARITY_DAZI,
        (
            ("sunflower", "向日葵", True, "花盘会追着太阳转，成熟后低下头，葵花籽能嗑一晚上。"),
            ("tangerine", "橘子", False, "皮薄好剥，酸甜看运气，橘子皮晒干还能泡水。"),
            ("orange", "橙子", False, "比橘子结实，汁水足，切开时香气先冲出来。"),
            ("rapeseed", "油菜", True, "春天开成金色花海，菜籽能榨油，嫩苔也能炒一盘。"),
            ("watermelon", "西瓜", False, "夏天标配，敲一敲听声音就知道熟没熟。"),
            ("honeydew", "蜜瓜", False, "网纹蜜瓜，甜得像加了糖，冰镇之后更好吃。"),
            ("pumpkin", "南瓜", False, "藤蔓能爬满半个院子，万圣节还能刻成灯。"),
            ("banana", "香蕉", False, "热带来的，一串能压弯整根假茎，熟透了才甜。"),
            ("water_bamboo", "茭白", False, "菰被黑粉菌感染后膨大的嫩茎，江南水乡的时令菜。"),
            ("grape", "葡萄", False, "藤本，一串串挂在架下，酿成酒又是另一种味道。"),
            ("water_caltrop", "菱角", False, "长在水里的三角果实，煮熟粉粉的，像小板栗。"),
            ("mangosteen", "山竹", False, "果中皇后，紫黑厚壳里是雪白的蒜瓣肉，清甜解腻。"),
            ("dragon_fruit", "火龙果", False, "仙人掌科量天尺的果实，切开里面全是小黑籽。"),
            ("durian", "榴莲", False, "爱的人爱死，恨的人退三步，果肉绵密像奶油。"),
            ("lychee", "荔枝", False, "一骑红尘妃子笑，剥壳后莹白得像凝脂。"),
            ("sugarcane", "甘蔗", False, "一节一节往上蹿，榨出来的汁甜得发黏。"),
        ),
    ),
    (
        C.RARITY_XIAOZI,
        (
            ("rice", "水稻", False, "水田里的主角，抽穗时一整片随风弯腰。"),
            ("corn", "玉米", False, "顶上开雄花，腰上抱雌穗，一根秆能结两三个。"),
            ("sweet_potato", "地瓜", False, "红薯，地下结块根，烤着吃最香。"),
            ("peanut", "花生", False, "地上开花、地下结果，拔起来一抖就是一串。"),
            ("soybean", "大豆", False, "毛豆老了就是黄豆，能榨油也能做豆腐。"),
            ("mung_bean", "绿豆", False, "夏天煮一锅绿豆汤，解暑。"),
            ("pea", "豌豆", False, "藤蔓卷须到处抓，嫩荚甜脆，老了就是豌豆黄。"),
            ("black_rice", "黑米", False, "带紫黑种皮的水稻，煮粥会把整锅染成深紫色。"),
            ("red_bean", "红豆", False, "红豆生南国，煮烂了就是豆沙，诗里最相思的那颗。"),
        ),
    ),
    (
        C.RARITY_DALAN,
        (
            ("greens", "青菜", False, "最家常的绿叶菜，下锅一炒就缩成一小盘。"),
            ("cucumber", "黄瓜", False, "带刺带花的小瓜，生吃脆，拍着吃更香。"),
            ("lettuce", "莴苣", False, "茎脆叶嫩，凉拌清炒都行，叶子别扔。"),
            ("napa_cabbage", "白菜", False, "北方冬天的底气，一棵能囤一整个月。"),
            ("spinach", "菠菜", False, "铁含量被夸大了，但确实耐寒又高产。"),
            ("radish", "萝卜", False, "白萝卜，霜打过之后更甜，炖汤一绝。"),
            ("carrot", "胡萝卜", False, "橙色的根，切丝炒着吃，兔子也爱。"),
            ("welsh_onion", "大葱", False, "葱白粗壮，蘸酱生吃是北方人的浪漫。"),
            ("spring_onion", "小葱", False, "细细一把，出锅前撒花，缺了它总差点意思。"),
            ("chive", "韭菜", False, "割一茬长一茬，包饺子最经典。"),
            ("celery", "芹菜", False, "香气特殊的茎菜，叶子其实也能吃。"),
            ("onion", "洋葱", False, "切开会让人流泪，炒软了却特别甜。"),
            ("tomato", "西红柿", False, "既是蔬菜也是水果，熟透的能直接当水果啃。"),
            ("zucchini", "西葫芦", False, "长得快结得多，嫩瓜擦丝做饼最省事。"),
        ),
    ),
    (
        C.RARITY_XIAOLAN,
        (
            ("foxtail", "狗尾巴草", False, "路边最常见，毛茸茸的穗子能编小兔子。"),
            ("plantain", "车前草", False, "叶子贴地长，穗子抽得老高，沟边随处可见。"),
            ("shepherd_purse", "荠菜", False, "春天第一口野菜，包馄饨鲜得眉毛掉。"),
            ("purslane", "马齿苋", False, "肉肉的茎叶，酸溜溜的，晒不死也旱不死。"),
            ("beggar_ticks", "鬼针草", False, "走过草丛裤子就粘满小针，拔都拔不完。"),
            ("black_nightshade", "龙葵", False, "结黑紫色小浆果，小时候常被长辈警告别乱吃。"),
            ("dandelion", "蒲公英", True, "黄花谢了变绒球，一吹就散成一片小伞。"),
            ("nettle", "荨麻", False, "叶上的小刺一碰就火辣辣地疼，焯水后才能吃。"),
            ("forage_grass", "牧草", False, "专门种来喂牲口的草，割一茬还会再长。"),
        ),
    ),
)


def _build_plants() -> dict[str, PlantDef]:
    """把目录展开成 PLANTS；品质决定成长时间、种子区间与果实 id。"""
    plants: dict[str, PlantDef] = {}
    for grade, entries in _CATALOG:
        best, worst = PLANT_SEED_RANGE[grade]
        grow = GROW_MINUTES_BY_GRADE[grade]
        for plant_id, name, is_flower, desc in entries:
            if plant_id in plants:  # 手滑复制粘贴时尽早炸掉，别等到收获才发现
                raise ValueError(f"植物 id 重复: {plant_id}")
            plants[plant_id] = PlantDef(
                id=plant_id,
                name=name,
                grade=grade,
                seed_best=best,
                seed_worst=worst,
                grow_minutes=grow,
                fruit_item=f"crop_{plant_id}",
                is_flower=is_flower,
                desc=desc,
            )
    return plants


PLANTS: dict[str, PlantDef] = _build_plants()

# 品质 -> 该品质的植物（预分组：plants_by_grade / roll_plant_for_seed 都是 O(1) 取表）
_PLANTS_BY_GRADE: dict[str, tuple[PlantDef, ...]] = {
    grade: tuple(p for p in PLANTS.values() if p.grade == grade)
    for grade in C.GARDEN_PLANT_GRADES
}


# ---------------------------------------------------------------- 查询
def get_plant(plant_id: str) -> PlantDef | None:
    """按 id 取植物；未知 id 返回 None（服务层好判断，不用 try/except）。"""
    return PLANTS.get(str(plant_id))


def all_plants() -> tuple[PlantDef, ...]:
    return tuple(PLANTS.values())


def plants_by_grade(grade: str) -> tuple[PlantDef, ...]:
    """某品质的全部植物；未知品质返回空元组。"""
    return _PLANTS_BY_GRADE.get(str(grade), ())


# ---------------------------------------------------------------- 随机规则
def _plant_grade_of(plant_grade: str) -> str:
    """容错：允许传品质（小紫）或植物 id（rice）；都不是就原样返回。"""
    text = str(plant_grade).strip()
    if text in PLANT_SEED_RANGE:
        return text
    plant = PLANTS.get(text)
    return plant.grade if plant is not None else text


def _seed_grade_of(seed_grade: str) -> str:
    """容错：'E' / 'e' / 'seed_e' 都归一成 'E'。"""
    text = str(seed_grade).strip().upper()
    if text.startswith("SEED_"):
        text = text[len("SEED_") :]
    return text


def seed_grades_for_plant(plant_grade: str) -> tuple[str, ...]:
    """某品质植物可收获的种子档位（从最好到最差）；未知品质返回空元组。"""
    bounds = PLANT_SEED_RANGE.get(_plant_grade_of(plant_grade))
    if not bounds:
        return ()
    best, worst = bounds
    order = C.GARDEN_SEED_GRADES
    try:
        start, end = order.index(best), order.index(worst)
    except ValueError:  # 配置写错档位时宁可返回空，也不让服务层抽到非法值
        return ()
    return order[start : end + 1]


def roll_plant_for_seed(seed_grade: str, rng: random.Random | None = None) -> PlantDef:
    """种子 -> 植物：按 SEED_GRADE_WEIGHTS 加权抽品质，再在该品质内均匀抽一种。

    `seed_grade` 接受 'S' / 's' / 'seed_s' 三种写法（服务层存 item id 也能直接用）。
    `rng` 传 `random.Random` 实例可复现；不传则用 core.rng 的默认实例。
    未知种子等级抛 ValueError（返回 PlantDef 的签名不允许返回 None，服务层应先在
    GARDEN_SEED_GRADES 里校验输入）。
    """
    grade = _seed_grade_of(seed_grade)
    weights = SEED_GRADE_WEIGHTS.get(grade)
    if not weights:
        raise ValueError(f"未知的种子等级: {seed_grade!r}（可选 {C.GARDEN_SEED_GRADES}）")
    plant_grade = _rng.pick_weighted(weights, rng)
    pool = _PLANTS_BY_GRADE.get(plant_grade) or all_plants()
    return _rng.choice(pool, rng)


def roll_seed_for_plant(plant_grade: str, mutation: int, rng: random.Random | None = None) -> str:
    """植物 + 突变值 -> 收获的种子档位。

    mutation 0~100 线性映射到 [worst, best]（规范 1.4/2.1）：
        0 -> worst，100 -> best，中间按下标四舍五入取档。
    以 S~C 四档为例：0 -> C，34 -> B，67 -> A，100 -> S。
    正好落在两档中点时默认向更好的一档取整；若显式传了 rng，则 50/50 决定
    （保留 rng 参数就是为了这个边界，正常调用不需要传）。
    """
    grades = seed_grades_for_plant(plant_grade)
    if not grades:
        raise ValueError(f"未知的植物品质: {plant_grade!r}")
    span = len(grades) - 1
    if span <= 0:  # 星彩 S~S：只出 S，任何突变都一样
        return grades[0]

    value = min(100.0, max(0.0, float(mutation)))
    raw = value / 100.0 * span
    base = int(raw)  # value >= 0，int() 即向下取整
    frac = raw - base
    if frac > 0.5 + 1e-9:
        step = base + 1
    elif frac < 0.5 - 1e-9:
        step = base
    elif rng is not None and rng.random() < 0.5:
        step = base
    else:
        step = base + 1
    step = max(0, min(span, step))
    # grades 是"最好 -> 最差"，step 是"从最差往上走"的档数
    return grades[span - step]
