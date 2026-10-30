"""建表语句（唯一真源）。

sql/schema.sql 是本文件的可读副本，供 DBA/文档使用；
运行期只认这里的字符串，避免打包丢文件导致启动失败。
"""

from __future__ import annotations

SCHEMA_VERSION = 6

SCHEMA_SQL = """
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
    feed_slots         TEXT    NOT NULL DEFAULT '',
    feed_total         INTEGER NOT NULL DEFAULT 0,
    feed_milestone     INTEGER NOT NULL DEFAULT 0,
    feed_auto_food     TEXT    NOT NULL DEFAULT '',  -- 自动购买偏好：rice_bowl / rice_pot
    work_ready_ts      INTEGER NOT NULL DEFAULT 0,
    work_started_ts    INTEGER NOT NULL DEFAULT 0,
    work_minutes       INTEGER NOT NULL DEFAULT 0,
    work_pending_coins INTEGER NOT NULL DEFAULT 0,
    fish_ready_ts      INTEGER NOT NULL DEFAULT 0,
    fish_bait          TEXT    NOT NULL DEFAULT '',
    fish_wait_min      INTEGER NOT NULL DEFAULT 0,
    fish_group         TEXT    NOT NULL DEFAULT '',
    bot_affection      INTEGER NOT NULL DEFAULT 0,  -- 对鲸鱼娘的好感（喂食累计）
    total_earned       INTEGER NOT NULL DEFAULT 0,
    total_spent        INTEGER NOT NULL DEFAULT 0,
    day_key            TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL DEFAULT '',
    updated_at         TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bags (
    uid        TEXT PRIMARY KEY,
    items      TEXT NOT NULL DEFAULT '{}',
    fish       TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ponds (
    group_id     TEXT    PRIMARY KEY,
    level        INTEGER NOT NULL DEFAULT 1,
    materials    TEXT    NOT NULL DEFAULT '{}',
    contribution TEXT    NOT NULL DEFAULT '{}',
    weather      TEXT    NOT NULL DEFAULT '',
    weather_until INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL DEFAULT ''
);

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

CREATE INDEX IF NOT EXISTS idx_harem_owner     ON harem(group_id, owner_uid);
CREATE INDEX IF NOT EXISTS idx_harem_affection ON harem(group_id, affection DESC);

CREATE TABLE IF NOT EXISTS buffs (
    subject_uid TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    group_id    TEXT    NOT NULL DEFAULT '',
    expires_ts  INTEGER NOT NULL DEFAULT 0,
    payload     TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (subject_uid, kind, group_id)
);

CREATE INDEX IF NOT EXISTS idx_buffs_expire ON buffs(expires_ts);

-- 注意：idx_users_fish 依赖 v2 新增的 fish_group 列，放在 database._migrate() 里建，
-- 否则旧库执行本脚本时会先报 "no such column: fish_group"，导致迁移根本没机会跑。

CREATE TABLE IF NOT EXISTS gardens (
    group_id        TEXT    NOT NULL,
    uid             TEXT    NOT NULL,
    seed_id         TEXT    NOT NULL DEFAULT '',
    plant_id        TEXT    NOT NULL DEFAULT '',
    planted_ts      INTEGER NOT NULL DEFAULT 0,
    growth          INTEGER NOT NULL DEFAULT 0,
    mutation        INTEGER NOT NULL DEFAULT 0,
    stage           INTEGER NOT NULL DEFAULT 0,
    watered_ts      INTEGER NOT NULL DEFAULT 0,
    watered_day     TEXT    NOT NULL DEFAULT '',
    water_count     INTEGER NOT NULL DEFAULT 0,
    fert_day        TEXT    NOT NULL DEFAULT '',
    fert_count      INTEGER NOT NULL DEFAULT 0,
    sun_day         TEXT    NOT NULL DEFAULT '',
    sun_ts          INTEGER NOT NULL DEFAULT 0,
    last_n          INTEGER NOT NULL DEFAULT 0,
    last_p          INTEGER NOT NULL DEFAULT 0,
    last_k          INTEGER NOT NULL DEFAULT 0,
    dead            INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT '',
    updated_at      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, uid)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);
"""