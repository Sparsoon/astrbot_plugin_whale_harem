-- ============================================================================
-- 赛博鲸鱼馆 · 数据库结构 v3（SQLite 3.35+ / WAL）
-- ----------------------------------------------------------------------------
-- 【为什么在 2G 机器上这样设计】
--   1. 只有 6 张表，全部走主键或现成索引，不存在笛卡尔积扫描；500 人 × 30 天的
--      数据量（约 15MB）远小于 page cache，热数据常驻内存，不靠大 buffer 硬扛。
--   2. 背包 / 池塘 / buff 的"多值字段"用 TEXT+JSON 存，而不是拆子表：
--      一次钓鱼只 UPDATE 一行、不加 join，省掉 SQLite 最贵的页分裂与随机 IO。
--   3. 所有"每日"状态都靠 day_key 文本字段惰性结算，没有定时任务、没有
--      重置批更新，凌晨 4 点不会出现写风暴。
--   4. 索引只建 3 条（见文末 CREATE INDEX），每条都是 (group_id, ...) 前缀，
--      写入放大极小；榜单类查询被 LIMIT 10 截断，扫描行数有上界。
--   5. WAL + busy_timeout 让"读"不被"写"堵住，5 条连接即可承担整群并发。
--
-- 【运行期唯一真源】core/db/schema.py::SCHEMA_SQL。
--   本文件的 DDL 必须与它逐字一致；改结构请先改 schema.py，再同步本文件。
--   本文件只用于 DBA 手工建库 / 审阅 / 迁移脚本比对，插件启动不读它。
--
-- 【JSON 字段结构契约】（UTF-8、无空白 separators、ensure_ascii=False）
--   bags.items
--       {"rice_bowl":3,"bait_normal":12,"card_wife":1}
--       键 = data/items.py 的 ItemDef.id；值 = 1..9999 的正整数（超上限按 9999 截断，
--       扣到 0 时该键会被删除，不允许出现 0 或负数）。
--   bags.fish
--       [{"sp":"li_yu","g":1800,"p":65,"ts":1761300000}]
--       契约：sp = data/fish.py 的鱼种 id（必填且非空，models.parse_bag 用它过滤脏数据）；
--       g = 重量(克)；p = 入护时的估值鲸币；ts = 入护 unix 秒。键名刻意用缩写，
--       80 条鱼约占 7KB；找不到的旧 sp 由 A3 在展示时兜底（fish.get 返回 None）。
--   ponds.materials
--       {"mat_stone":12,"mat_coral":3,"mat_bone":24,"mat_crystal":8}
--       键 = 材料 ItemDef.id（mat_*）；值 = 池中存量正整数，升级扣到 0 即删键。
--   ponds.contribution
--       {"123456789":20,"987654321":5}
--       键 = 群友 uid（TEXT，非昵称）；值 = 该群友累计投入的材料个数（不衰减）。
--   buffs.payload
--       {"mult":2,"level":1}
--       注意：Repo.get_buff 用 models.parse_int_dict 解析，**只保留 int 值**，
--       字符串 / 浮点会被静默丢弃；需要复杂结构请改成 kv 表存 JSON。
--   kv.v
--       任意 TEXT。约定：结构化值以 "json:" 开头，纯字符串直接存。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 建议的连接级 PRAGMA。运行期由 core/db/database.py::_PRAGMAS 对每条连接逐一
-- 应用（本段只是给 DBA 手工建库/巡检用，与代码保持一致）。
-- ---------------------------------------------------------------------------
PRAGMA journal_mode=WAL;        -- 读写并发：读不阻塞写，写不阻塞读
PRAGMA synchronous=NORMAL;      -- WAL 下断电最多丢最近 1 个事务，比 FULL 快一个量级
PRAGMA busy_timeout=5000;       -- 抢不到锁就等 5s，而不是立刻报 database is locked
PRAGMA cache_size=-2000;        -- 每条连接 2MB page cache（负数 = KB），5 条约 10MB
PRAGMA temp_store=MEMORY;       -- 排序/临时表放内存，2G 机器上临时表都很小
PRAGMA mmap_size=0;             -- 关闭 mmap，避免 RSS 虚高被 OOM Killer 盯上
PRAGMA foreign_keys=ON;         -- 当前无外键，先开着给未来迁移留余地
PRAGMA wal_autocheckpoint=200;  -- 每 200 页自动 checkpoint，WAL 不会无限膨胀

-- ---------------------------------------------------------------------------
-- users：一人一行，所有"每日"计数都靠 day_key / feed_day 惰性清零。
--   uid              平台用户号（TEXT，全局唯一，主键）
--   name             最近一次见到的昵称（会被覆盖，不保证唯一）
--   coins            鲸币余额（允许负数：先扣后还的场景由 service 自行保证）
--   level / exp      等级与当前等级内经验（升级曲线 100*level，最高 99 级）
--   checkin_streak   连续签到的"游戏日"计数（断签由 service 清零）
--   last_checkin_day 上次签到日 day_key，用于判连续与补签
--   total_checkin    累计签到次数（成就/统计用，永不重置）
--   feed_day         喂食计数所属游戏日；与 day_key 分开是为了喂食单独重置
--   feed_count       当日喂食次数（> FEED_DAILY_SOFT_LIMIT 后收益衰减）
--   feed_reward_day  当日喂食里程碑奖励是否已发的标记日
--   work_ready_ts    本次上工的收工时间戳（0 = 没在上工）
--   work_started_ts  上工时间戳（等待模型用）
--   work_minutes     本次班时长（分钟），收益 = 时长 × 0.9 × 浮动，暴击 ×1.8
--   work_pending_coins 上工时算好的工资，>0 表示待收工/待领钱
--   fish_ready_ts    可收竿时间戳（0 = 没抛竿）；等待期间不能钓鱼/打工
--   fish_bait        本次抛竿消耗的饵料 id（收竿时取 tier 算品质）
--   fish_wait_min    本次等待分钟数（5~60，展示用）
--   total_earned     累计赚取鲸币（只增不减，用于统计与反作弊对账）
--   total_spent      累计花费鲸币（只增不减）
--   day_key          用户级惰性结算游标：与 today 不一致即视为跨天
--   created_at/updated_at  本地时间 "YYYY-MM-DD HH:MM:SS"
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    uid                TEXT    PRIMARY KEY,
    name               TEXT    NOT NULL DEFAULT '',
    coins              INTEGER NOT NULL DEFAULT 0,
    level              INTEGER NOT NULL DEFAULT 1,
    exp                INTEGER NOT NULL DEFAULT 0,
    checkin_streak     INTEGER NOT NULL DEFAULT 0,
    last_checkin_day   TEXT    NOT NULL DEFAULT '',
    total_checkin      INTEGER NOT NULL DEFAULT 0,
    feed_day           TEXT    NOT NULL DEFAULT '',
    feed_count         INTEGER NOT NULL DEFAULT 0,
    feed_reward_day    TEXT    NOT NULL DEFAULT '',
    work_ready_ts      INTEGER NOT NULL DEFAULT 0,
    work_started_ts    INTEGER NOT NULL DEFAULT 0,
    work_minutes       INTEGER NOT NULL DEFAULT 0,
    work_pending_coins INTEGER NOT NULL DEFAULT 0,
    fish_ready_ts      INTEGER NOT NULL DEFAULT 0,
    fish_bait          TEXT    NOT NULL DEFAULT '',
    fish_wait_min      INTEGER NOT NULL DEFAULT 0,
    fish_group         TEXT    NOT NULL DEFAULT '',
    bot_affection      INTEGER NOT NULL DEFAULT 0,
    total_earned       INTEGER NOT NULL DEFAULT 0,
    total_spent        INTEGER NOT NULL DEFAULT 0,
    day_key            TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL DEFAULT '',
    updated_at         TEXT    NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- 咬钩检查：只在群消息到达时按 (群, 可收竿时间) 找待收竿的鱼
-- 注意：此索引依赖 v2 的 fish_group 列；运行期由 database._migrate() 在补列后创建。
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_users_fish ON users(fish_group, fish_ready_ts);

-- ---------------------------------------------------------------------------
-- bags：一人一行，物品与鱼护都塞进 JSON（见文件头契约）。
--   uid        用户号（主键，与 users.uid 同值；不做外键，删除用户时由
--              Repo.delete_all_of_user 显式清理，避免级联删除带来的额外锁）
--   items      TEXT JSON 对象：{item_id: 数量}
--   fish       TEXT JSON 数组：[{sp, name, rarity, w, ts}, ...]
--   updated_at 最后一次写入时间（只用于运维排查，不参与业务判断）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bags (
    uid        TEXT PRIMARY KEY,
    items      TEXT NOT NULL DEFAULT '{}',
    fish       TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- ponds：一群一行，材料与贡献度都是 JSON。
--   group_id     群号（主键）
--   level        池塘等级 1..POND_MAX_LEVEL(10)，越界值在 Pond.from_row 被夹紧
--   materials    TEXT JSON：{material_id: 存量}（升级时按 POND_UPGRADE_REQUIREMENTS 扣）
--   contribution TEXT JSON：{uid: 累计捐献材料数}，只增不减
--   updated_at   最后写入时间
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ponds (
    group_id     TEXT    PRIMARY KEY,
    level        INTEGER NOT NULL DEFAULT 1,
    materials    TEXT    NOT NULL DEFAULT '{}',
    contribution TEXT    NOT NULL DEFAULT '{}',
    updated_at   TEXT    NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- harem：一行 = (群, 目标群友) 的关系状态，公共老婆就是 owner_uid=''。
--   group_id     群号
--   target_uid   被攻略/被娶的目标群友 uid（与 group_id 组成复合主键）
--   target_name  目标昵称（展示用，会被最新昵称覆盖）
--   owner_uid    拥有者 uid；空串 = 公共老婆（可攻略）
--   owner_name   拥有者昵称（展示用，避免每次 join users）
--   affection    好感度（每日惰性衰减，见 settle_harem_day）
--   marry_fail   当日求婚失败次数（保底计数，跨天清零）
--   ntr_fail     当日 NTR 失败次数（保底计数）
--   divorce_fail 当日离婚失败次数（保底计数）
--   ntr_count    累计被 NTR 成功次数（只增，绿帽榜排序键）
--   chase_count  累计被追求次数（只增，公共老婆榜排序键）
--   day_key      该行保底计数的结算游标（惰性清零）
--   created_at   关系建立时间（"YYYY-MM-DD HH:MM:SS"，也是娶入顺序的排序键）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS harem (
    group_id     TEXT    NOT NULL,
    target_uid   TEXT    NOT NULL,
    target_name  TEXT    NOT NULL DEFAULT '',
    owner_uid    TEXT    NOT NULL DEFAULT '',
    owner_name   TEXT    NOT NULL DEFAULT '',
    affection    INTEGER NOT NULL DEFAULT 0,
    marry_fail   INTEGER NOT NULL DEFAULT 0,
    ntr_fail     INTEGER NOT NULL DEFAULT 0,
    divorce_fail INTEGER NOT NULL DEFAULT 0,
    ntr_count    INTEGER NOT NULL DEFAULT 0,
    chase_count  INTEGER NOT NULL DEFAULT 0,
    day_key      TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, target_uid)
);

-- 后宫列表 / 人数统计：WHERE group_id=? AND owner_uid=? —— 等值前缀，命中即止
CREATE INDEX IF NOT EXISTS idx_harem_owner     ON harem(group_id, owner_uid);
-- 好感度榜：WHERE group_id=? ORDER BY affection DESC LIMIT 10 —— 索引即排序
CREATE INDEX IF NOT EXISTS idx_harem_affection ON harem(group_id, affection DESC);

-- ---------------------------------------------------------------------------
-- buffs：时效性增益（体验券 / 钟情卡护盾）。过期靠"读时判断 + 启动清理"，
--        没有任何定时器。
--   subject_uid 生效对象 uid（护盾卡挂在被保护者身上）
--   kind        buff 种类字符串，如 "ticket_reservoir" / "card_loyal"
--   group_id    作用域群号；'' = 全局生效（私人增益）
--   expires_ts  过期 unix 时间戳；<= now 即视为失效并惰性删除
--   payload     TEXT JSON，仅 int 值有效（见文件头说明）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS buffs (
    subject_uid TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    group_id    TEXT    NOT NULL DEFAULT '',
    expires_ts  INTEGER NOT NULL DEFAULT 0,
    payload     TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (subject_uid, kind, group_id)
);

-- 启动时一次性 DELETE FROM buffs WHERE expires_ts<=now；active_buffs 也用它
CREATE INDEX IF NOT EXISTS idx_buffs_expire ON buffs(expires_ts);

-- ---------------------------------------------------------------------------
-- kv：杂项小状态（全服公告、版本标记、全局开关）。行数应保持在两位数。
--   k 键；v 值（TEXT，结构化值以 "json:" 前缀约定）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- 结构版本号：database.py::_check_version 读取/写入，升级迁移靠它判断。
-- 注意：本语句只在"全新建库"时安全；对已有库执行请确认 user_version 不回退。
-- ---------------------------------------------------------------------------
PRAGMA user_version=3;