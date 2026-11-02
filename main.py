"""赛博鲸鱼馆 · AstrBot 插件入口。

职责边界（很重要，别把业务塞进来）：
    * 只做「事件 -> 参数 -> service -> Card -> 图片」的管线；
    * 统一兜底：业务异常变错误卡，未知异常写日志后静默，绝不让异常冒到 AstrBot 核心；
    * 统一刹车：单用户防抖 + 按 uid 串行锁 + 服务级超时 + 渲染并发闸门；
    * 所有指令都返回一张图（Pillow 渲染失败时退化为纯文本，保证有回应）。

2 核 2G 关键点：
    1. 插件加载时不建库、不建图，全部在 initialize() 里懒启动，失败也不阻塞 AstrBot 启动；
    2. 每个指令只做一次 `_precheck`（读 sender/group），不重复查库；
    3. `asyncio.wait_for` 包住每个业务调用，防止某个 service 卡死把事件循环拖垮。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 加载方式兼容（很重要，别删）
# AstrBot 有两种加载插件的路径：按包 import（`<插件目录名>.main`）或直接按文件路径
# exec main.py。后者的 `__package__` 是空串，`from .core import ...` 会抛
# "attempted relative import with no known parent package"，整插件直接加载失败。
# 这里在**任何相对导入之前**补一个同名的合成包，把 __package__ 填上，两种方式都能跑。
# ---------------------------------------------------------------------------
import sys as _sys
import types as _types
from pathlib import Path as _Path

_CANONICAL_PACKAGE = "astrbot_plugin_whale_harem"
if __package__ in (None, ""):
    _here = _Path(__file__).resolve().parent
    _parent = str(_here.parent)
    if _parent not in _sys.path:
        _sys.path.insert(0, _parent)
    if _CANONICAL_PACKAGE not in _sys.modules:
        _fake = _types.ModuleType(_CANONICAL_PACKAGE)
        _fake.__path__ = [str(_here)]  # type: ignore[attr-defined]
        _sys.modules[_CANONICAL_PACKAGE] = _fake
    __package__ = _CANONICAL_PACKAGE

import asyncio
import os
import re
import time
from typing import Any, Awaitable, Callable

from astrbot.api import logger as _logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot >= 3.5.19 起 @register 已废弃（未来版本会移除），这里做兼容导入
    from astrbot.api.star import register
except ImportError:  # pragma: no cover - 未来版本
    def register(*_args, **_kwargs):  # type: ignore[misc]
        return lambda cls: cls

from .core.async_utils import Debouncer, KeyedLock
from .core.config import PluginConfig
from .core.constants import LIFT_CHANCE, PLUGIN_NAME
from .core.context import SvcCtx
from .core.db.database import Database
from .core.db.repos import Repo
from .core.errors import WhaleError
from .core.llm import LLMClient
from .core.paths import cache_dir as get_cache_dir
from .core.paths import db_file
from .core.rng import chance, choice
from .core.time_utils import day_key, fmt_duration, now_ts, seconds_to_reset
from .data import items as item_db
from .services import (
    daily,
    feed,
    fishing,
    items as item_svc,
    marriage,
    pond,
    profile,
    shop,
    warehouse,
    work,
    garden,
)
from .ui import Renderer
from .ui.schema import Card, card, error_card, kv_block, kv, list_block, stat

log = _logger

_SERVICE_TIMEOUT = 20.0

# "打工"相关指令：监听器不要在用户主动查打工时抢着结算（否则会同时回两张卡）
_WORK_COMMANDS = frozenset({"打工", "上班", "摸鱼", "收工", "work"})

# 指令失败时给出的"正确用法"示例（错误卡最后一行）
_USAGE: dict[str, tuple[str, ...]] = {
    "签到": ("/签到",),
    "补签": ("/补签（消耗 1 张补签卡）",),
    "喂食": ("/喂食", "/喂食 自动 白米饭"),
    "打工": ("/打工（上工）", "/打工（到点收工）"),
    "钓鱼": ("/钓鱼 红虫",),
    "背包": ("/背包",),
    "仓库": ("/仓库",),
    "卖鱼": ("/卖鱼", "/卖鱼 杂物"),
    "池塘": ("/池塘",),
    "捐献": ("/捐献 基石", "/捐献 all"),
    "娶群友": ("/娶群友 @TA",),
    "离婚": ("/离婚 @TA",),
    "ntr": ("/ntr @TA",),
    "商店": ("/商店",),
    "购买": ("/购买 红虫 3",),
    "使用": ("/使用 白米饭", "/使用 钟情卡 @TA"),
    "我的": ("/我的",),
    "帮助": ("/帮助",),
    "鲸鱼馆": ("/鲸鱼馆",),
    "养植物": ("/养植物",),
    "花园": ("/养植物",),
    "播种": ("/播种", "/播种 E 级种子"),
    "浇水": ("/浇水",),
    "施肥": ("/施肥 氮", "/施肥 磷", "/施肥 钾"),
    "晒太阳": ("/晒太阳",),
    "收获": ("/收获",),
}


def _usage_for(event: AstrMessageEvent) -> tuple[str, ...]:
    """从消息里取指令名，返回对应的正确用法示例。"""
    text = (getattr(event, "message_str", "") or "").strip().lstrip("/／").strip()
    if not text:
        return ("/帮助",)
    head = text.split()[0].lower()
    return _USAGE.get(head, ("/帮助",))

CARD = Callable[[SvcCtx, str, str, str], Awaitable[Card]]


@register(
    "astrbot_plugin_whale_harem",
    "煤球",
    "赛博鲸鱼馆：群共享池塘钓鱼、签到喂食打工、娶群友与后宫。全异步 + Pillow 出图 + 惰性重置。",
    "1.0.0",
)
class WhaleHaremPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        # 4.9 的 Star.__init__ 只收 context，新版多一个 config；两种都兼容
        try:
            super().__init__(context, config)  # type: ignore[arg-type]
        except TypeError:
            super().__init__(context)
        self.context = context
        self.cfg = PluginConfig.from_dict(config or {})
        self._db: Database | None = None
        self._repo: Repo | None = None
        self._ctx: SvcCtx | None = None
        self._renderer: Renderer | None = None
        self._debouncer = Debouncer(self.cfg.command_debounce_ms)
        self._keyed = KeyedLock()
        self._init_lock = asyncio.Lock()
        self._ready = False
        self._init_retry_after = 0.0

    # ================================================================ 生命周期
    async def initialize(self) -> None:
        """启动钩子。初始化失败**不能**让插件加载失败（否则整个插件从列表消失）。"""
        try:
            await self._ensure_ready()
        except Exception as exc:  # noqa: BLE001
            log.error(f"赛博鲸鱼馆初始化失败（插件仍加载，指令会提示稍后重试）: {exc!r}")

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        if time.monotonic() < self._init_retry_after:
            # 30 秒内不重复尝试建库，避免磁盘故障时每条指令都去撞一次超时
            raise WhaleError("鲸鱼馆正在维修", "初始化失败，请 30 秒后再试")
        async with self._init_lock:
            if self._ready:
                return
            try:
                cfg = self.cfg
                # 统一日切时区：花园的"每天 4 点"和晒太阳窗口使用同一个时区
                try:
                    if cfg.garden_timezone:
                        os.environ["TZ"] = str(cfg.garden_timezone)
                        if hasattr(time, "tzset"):
                            time.tzset()
                except Exception as exc:  # noqa: BLE001 - Windows/精简系统没有 tzset
                    log.debug(f"设置时区失败，沿用系统时区: {exc!r}")
                db_path = db_file(PLUGIN_NAME, cfg.db_path)
                self._db = Database(db_path, max_connections=5)
                await self._db.init()
                self._repo = Repo(self._db, cfg)
                # 启动时清一次过期 buff（不是轮询，只在加载时执行一次）
                await self._repo.expire_buffs()
                self._ctx = SvcCtx(repo=self._repo, cfg=cfg, llm=LLMClient(self.context, cfg))
                try:
                    # 字体探测 / 目录创建都是同步 IO，丢线程池避免卡住启动时的事件循环
                    self._renderer = await asyncio.to_thread(
                        Renderer, cfg, get_cache_dir(PLUGIN_NAME), PLUGIN_NAME
                    )
                except Exception as exc:  # noqa: BLE001 - 字体/依赖缺失时降级为纯文字
                    log.error(f"渲染器初始化失败，将降级为文字回复: {exc!r}")
                    self._renderer = None
                self._ready = True
                log.info(f"赛博鲸鱼馆启动完成 | {cfg.safe_summary()} | db={db_path}")
            except Exception:
                self._init_retry_after = time.monotonic() + 30.0
                raise

    async def terminate(self) -> None:
        """插件卸载/停用：按渲染器 -> 数据库的顺序释放，避免任务还在用连接。"""
        try:
            if self._renderer is not None:
                await self._renderer.close()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"关闭渲染器异常: {exc!r}")
        try:
            if self._db is not None:
                await self._db.close()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"关闭数据库异常: {exc!r}")
        self._ready = False
        log.info("赛博鲸鱼馆已卸载")

    # ================================================================ 事件工具
    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_group_id", None)
        gid: Any = None
        if callable(getter):
            try:
                gid = getter()
            except Exception:  # noqa: BLE001
                gid = None
        if not gid:
            gid = getattr(getattr(event, "message_obj", None), "group_id", "") or ""
        return str(gid)

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        msg = getattr(event, "message_obj", None)
        for attr in ("self_id", "self_id_str"):
            value = getattr(msg, attr, None)
            if value:
                return str(value)
        return ""

    @classmethod
    def _target(cls, event: AstrMessageEvent) -> tuple[str, str]:
        """从消息链里取第一个有效的 @ 目标（排除自己和 @全体）。"""
        self_id = cls._self_id(event)
        msg = getattr(event, "message_obj", None)
        chain = getattr(msg, "message", None) or []
        for seg in chain:
            qq = getattr(seg, "qq", None)
            if qq is None:
                continue
            qq_str = str(qq)
            if qq_str in ("all", "0", "", self_id):
                continue
            if not qq_str.isdigit():
                continue
            name = getattr(seg, "name", "") or ""
            return qq_str, str(name)[:32]
        return "", ""

    @staticmethod
    def _args(event: AstrMessageEvent) -> str:
        """把 `/购买 红虫 3` / `购买 红虫 3` 里的命令词剥掉，只留参数。"""
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return ""
        text = text.lstrip("/").lstrip("／").strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _split_count(arg: str) -> tuple[str, int]:
        """'红虫 3' -> ('红虫', 3)；'红虫x3' / '红虫3' 也认（供扩展用）。"""
        arg = (arg or "").strip()
        if not arg:
            return "", 1
        normalized = arg.replace("x", " ").replace("X", " ").replace("*", " ").replace("×", " ")
        match = re.search(r"(\d{1,2})\s*(?:个|份|张|条|包)?$", normalized)
        if match:
            num = int(match.group(1))
            head = normalized[: match.start()].strip(" ，,")
            return head or arg, max(1, min(99, num))
        return arg, 1

    # ================================================================ 统一管线
    async def _run(
        self,
        event: AstrMessageEvent,
        handler: CARD,
        *,
        need_target: bool = False,
        optional_target: bool = False,
        extra: tuple[Any, ...] = (),
    ) -> Any:
        """所有指令的唯一入口：前置校验 -> 防抖 -> 串行 -> 超时 -> 错误卡 -> 出图。"""
        try:
            await self._ensure_ready()
        except Exception as exc:  # noqa: BLE001
            log.error(f"鲸鱼馆初始化失败: {exc!r}")
            return await self._emit(
                event,
                error_card(
                    "数据库还没准备好，指令暂时用不了",
                    "插件会每 30 秒自动重试一次；等一会儿再发。持续不行请管理员看日志",
                    ("/帮助",),
                    title="正在重试初始化",
                ),
            )

        gid = self._group_id(event)
        uid = str(event.get_sender_id() or "")
        if not uid:
            return None
        if gid:
            if not self.cfg.group_enabled(gid):
                return None
        else:
            # 私聊：用 private:<uid> 作为独立作用域，池塘/钓鱼/打工都能单机测试
            gid = f"private:{uid}"
        name = str(event.get_sender_name() or "群友")[:32]

        target_uid, target_name = (
            self._target(event) if (need_target or optional_target) else ("", "")
        )
        if need_target and not target_uid:
            return await self._emit(
                event,
                error_card(
                    "这个操作需要先 @ 一位群友",
                    "在群里点一下对方头像（或输入 @昵称），再发送指令",
                    _usage_for(event),
                    title="还差一个对象",
                ),
            )

        if not self._debouncer.allow(uid):
            # 连点静默丢弃：不回复 = 最省资源
            return None

        ctx = self._ctx
        assert ctx is not None
        try:
            async with self._keyed.hold(uid):
                result = await asyncio.wait_for(
                    handler(ctx, uid, name, gid, target_uid, target_name, *extra),
                    timeout=_SERVICE_TIMEOUT,
                )
        except asyncio.TimeoutError:
            log.warning(f"指令超时 uid={uid} handler={getattr(handler, '__name__', '?')}")
            return await self._emit(
                event,
                error_card(
                    "这条指令跑了 20 秒还没结束，已经中断",
                    "通常是同一时间人太多；等 10 秒再发一次就好",
                    _usage_for(event),
                    title="处理超时",
                ),
            )
        except WhaleError as exc:
            return await self._emit(
                event,
                error_card(
                    exc.message,
                    exc.hint,
                    tuple(exc.commands) or _usage_for(event),
                    title="这条指令没走通",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.error(f"指令异常 uid={uid}: {exc!r}")
            return await self._emit(
                event,
                error_card(
                    "插件内部出了点问题，这条指令没有生效",
                    "先按下面的示例重试一次；如果一直这样，把这张图发给管理员",
                    _usage_for(event),
                    title="出了点小故障",
                ),
            )

        if result is None:
            return None
        return await self._emit(event, result)

    async def _render_path(self, card_obj: Card) -> str:
        """只出图不包装消息：监听器 / 指令层共用。

        失败时先把卡片压成紧凑版重试一次，仍失败才降级文字；日志带卡片信息，方便定位。
        """
        if self._renderer is None:
            return ""
        try:
            return await asyncio.wait_for(self._renderer.render(card_obj), timeout=40.0)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"渲染失败，尝试紧凑重试: title={card_obj.title!r} "
                f"owner={card_obj.owner!r} blocks={len(card_obj.blocks)} err={exc!r}"
            )
        try:
            return await asyncio.wait_for(self._renderer.render(card_obj.compact()), timeout=25.0)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"紧凑卡也渲染失败，降级文字: title={card_obj.title!r} "
                f"owner={card_obj.owner!r} err={exc!r}"
            )
            return ""

    async def _emit(self, event: AstrMessageEvent, card_obj: Card) -> Any:
        """Card -> 图片路径 -> 消息结果；渲染失败退化为文字。"""
        if not card_obj.owner:
            card_obj.owner = str(event.get_sender_name() or "")[:16]
        path = await self._render_path(card_obj)
        if path:
            return event.image_result(path)
        return event.plain_result(card_obj.to_text(900))

    # ================================================================ 指令
    # ---- 基础
    @filter.command("签到", alias={"checkin", "打卡"})
    async def cmd_checkin(self, event: AstrMessageEvent):
        """每日签到：50 鲸币起，连续签到递增，附赠随机饵料。"""
        return await self._run(event, lambda c, u, n, g, *_: daily.do_checkin(c, u, n, g))

    @filter.command("补签", alias={"makeup"})
    async def cmd_makeup(self, event: AstrMessageEvent):
        """消耗一张补签卡，把昨天漏掉的签到补回来。"""
        return await self._run(event, lambda c, u, n, g, *_: daily.do_makeup(c, u, n, g))

    @filter.command("喂食", alias={"投喂", "feed"})
    async def cmd_feed(self, event: AstrMessageEvent):
        """投喂鲸鱼娘本人：涨她的好感、随机返鲸币，每日第 5 次有额外奖励。"""
        return await self._run(
            event,
            lambda c, u, n, g, *_: feed.do_feed(
                c, u, n, g, event.unified_msg_origin, self._args(event)
            ),
        )

    @filter.command("打工", alias={"上班", "摸鱼", "收工", "work"})
    async def cmd_work(self, event: AstrMessageEvent):
        """上工后等随机时长，再发一次收工领工资（收益与时长成正比）。"""
        return await self._run(event, lambda c, u, n, g, *_: work.do_work(c, u, n, g))

    # ---- 钓鱼
    @filter.command("钓鱼", alias={"甩竿", "钓", "收竿", "fish"})
    async def cmd_fish(self, event: AstrMessageEvent):
        """抛竿消耗 1 个饵料，等 5~60 分钟后再发一次收竿。"""
        arg = self._args(event)
        bait_id = ""
        if arg:
            found = item_db.resolve_item(arg)
            if found and item_db.is_bait(found.id):
                bait_id = found.id
        return await self._run(
            event,
            lambda c, u, n, g, *_: fishing.do_fish(c, u, n, g, bait_id or None, event.unified_msg_origin),
        )

    @filter.command("仓库", alias={"储物间", "warehouse"})
    async def cmd_warehouse(self, event: AstrMessageEvent):
        """按类别查看仓库里的物品、数量与估值（只读）。"""
        return await self._run(event, lambda c, u, n, g, *_: warehouse.do_warehouse(c, u, n, g))

    @filter.command("背包", alias={"鱼护", "我的鱼", "bag"})
    async def cmd_bag(self, event: AstrMessageEvent):
        """查看背包与鱼护。"""
        return await self._run(event, lambda c, u, n, g, *_: fishing.do_bag(c, u, n, g))

    @filter.command("卖鱼", alias={"出售", "sell"})
    async def cmd_sell(self, event: AstrMessageEvent):
        """卖鱼 / 卖杂物 / 卖指定物品。"""
        arg = self._args(event)
        return await self._run(event, lambda c, u, n, g, *_: fishing.do_sell(c, u, n, g, arg))

    # ---- 池塘
    @filter.command("池塘", alias={"看池塘", "pond"})
    async def cmd_pond(self, event: AstrMessageEvent):
        """查看本群共用池塘的等级、加成与升级进度。"""
        return await self._run(event, lambda c, u, n, g, *_: pond.do_pond(c, u, n, g))

    @filter.command("捐献", alias={"池塘捐献", "捐材料"})
    async def cmd_donate(self, event: AstrMessageEvent):
        """向群池塘捐献升级材料（材料只能钓鱼获得，商店买不到）。"""
        arg = self._args(event)
        return await self._run(event, lambda c, u, n, g, *_: pond.do_donate(c, u, n, g, arg))

    # ---- 社交
    @filter.command("娶群友", alias={"娶妻", "求婚", "marry"})
    async def cmd_marry(self, event: AstrMessageEvent):
        """消耗 150 鲸币或娶群友卡，10% 起，每次 +10%，5 次必中。"""
        arg = self._args(event)
        return await self._run(
            event,
            lambda c, u, n, g, tu, tn, *_: marriage.do_marry(c, u, n, g, tu, tn, "卡" in arg),
            need_target=True,
        )

    @filter.command("离婚", alias={"分手", "divorce"})
    async def cmd_divorce(self, event: AstrMessageEvent):
        """消耗 150 鲸币或好人卡，50% 成功，2 次必中。"""
        arg = self._args(event)
        return await self._run(
            event,
            lambda c, u, n, g, tu, tn, *_: marriage.do_divorce(c, u, n, g, tu, tn, "卡" in arg),
            need_target=True,
        )

    @filter.command("ntr", alias={"NTR", "绿帽", "牛头人"})
    async def cmd_ntr(self, event: AstrMessageEvent):
        """消耗 200 鲸币或绿帽卡，5% 起，每次 +5%，10 次必中。"""
        arg = self._args(event)
        return await self._run(
            event,
            lambda c, u, n, g, tu, tn, *_: marriage.do_ntr(c, u, n, g, tu, tn, "卡" in arg),
            need_target=True,
        )

    @filter.command("我的后宫", alias={"后宫", "harem"})
    async def cmd_harem(self, event: AstrMessageEvent):
        """查看后宫，默认最近 10 条。"""
        return await self._run(event, lambda c, u, n, g, *_: marriage.do_harem(c, u, n, g))

    @filter.command("恩爱榜", alias={"恩爱排行", "love"})
    async def cmd_love(self, event: AstrMessageEvent):
        """本群恩爱度排行。"""
        return await self._run(event, lambda c, u, n, g, *_: marriage.do_love_ranking(c, g))

    @filter.command("公共老婆", alias={"公共", "public"})
    async def cmd_public(self, event: AstrMessageEvent):
        """群里的公共老婆 / 被抢最多的目标。"""
        return await self._run(event, lambda c, u, n, g, *_: marriage.do_public_wives(c, g))

    # ---- 商店 / 道具
    @filter.command("商店", alias={"鲸鱼商店", "shop"})
    async def cmd_shop(self, event: AstrMessageEvent):
        """商店：鱼饵、钟情卡、白饭、池塘体验券。"""
        return await self._run(event, lambda c, u, n, g, *_: shop.do_shop(c, u, n, g))

    @filter.command("购买", alias={"买", "buy"})
    async def cmd_buy(self, event: AstrMessageEvent):
        """购买物品：/购买 红虫 3"""
        arg = self._args(event)
        return await self._run(event, lambda c, u, n, g, *_: shop.do_buy(c, u, n, g, arg))

    @filter.command("使用", alias={"用", "use"})
    async def cmd_use(self, event: AstrMessageEvent):
        """使用道具：/使用 补签卡、/使用 钟情卡 @某人、/使用 水库一天体验券"""
        arg = self._args(event)
        return await self._run(
            event,
            lambda c, u, n, g, tu, tn, *_: item_svc.do_use(
                c, u, n, g, arg, tu, tn, event.unified_msg_origin
            ),
            optional_target=True,
        )

    # ---- 面板
    @filter.command("我的", alias={"面板", "鲸鱼面板", "me", "profile"})
    async def cmd_profile(self, event: AstrMessageEvent):
        """个人面板：鲸币、等级、签到、后宫、池塘、在手 buff。"""
        return await self._run(event, lambda c, u, n, g, *_: profile.do_profile(c, u, n, g))

    @filter.command("帮助", alias={"鲸鱼帮助", "help", "菜单"})
    async def cmd_help(self, event: AstrMessageEvent):
        """指令一览。"""
        return await self._emit(event, self._help_card())

    @filter.command("鲸鱼馆", alias={"stats"})
    async def cmd_stats(self, event: AstrMessageEvent):
        """运行状态（管理员视角，普通群友也能看）。"""
        return await self._emit(event, await self._stats_card(event))



    @filter.command("养植物", alias={"花园", "garden"})
    async def cmd_garden(self, event: AstrMessageEvent):
        """花园总览 / 帮助：植物状态、成长进度、可做的事。"""
        return await self._run(event, lambda c, u, n, g, *_: garden.do_garden(c, u, n, g))

    @filter.command("播种", alias={"种植", "sow"})
    async def cmd_sow(self, event: AstrMessageEvent):
        """播种：不带参数列出种子；带参数种下指定种子。"""
        return await self._run(
            event,
            lambda c, u, n, g, *_: garden.do_sow(c, u, n, g, self._args(event)),
        )

    @filter.command("浇水", alias={"water"})
    async def cmd_water(self, event: AstrMessageEvent):
        """浇水：每 1 小时一次，面板尽量窄。"""
        return await self._run(event, lambda c, u, n, g, *_: garden.do_water(c, u, n, g))

    @filter.command("施肥", alias={"fertilize"})
    async def cmd_fertilize(self, event: AstrMessageEvent):
        """施肥：每天最多 2 次，不同肥料成长/突变不同。"""
        return await self._run(
            event,
            lambda c, u, n, g, *_: garden.do_fertilize(c, u, n, g, self._args(event)),
        )

    @filter.command("晒太阳", alias={"sun"})
    async def cmd_sun(self, event: AstrMessageEvent):
        """晒太阳：每天 1 次，7-19 点（可配置），与天气联动。"""
        return await self._run(event, lambda c, u, n, g, *_: garden.do_sun(c, u, n, g))

    @filter.command("收获", alias={"harvest"})
    async def cmd_harvest(self, event: AstrMessageEvent):
        """收获：结果期顶部后可收，得到果实与一颗种子。"""
        return await self._run(event, lambda c, u, n, g, *_: garden.do_harvest(c, u, n, g))

    # ================================================================ 群聊触发
    @staticmethod
    def _is_work_command(event: AstrMessageEvent) -> bool:
        text = (getattr(event, "message_str", "") or "").strip().lstrip("/／").strip()
        if not text:
            return False
        return text.split()[0] in _WORK_COMMANDS

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_activity(self, event: AstrMessageEvent):
        """群聊驱动的事件（零轮询）：

        1. 打工：**到点后本人发一条消息**才结算工资（跳过 /打工 指令，避免双卡）；
        2. 钓鱼：**到点后任何群消息**都有 25% 概率触发咬钩，随机选一条待收竿的鱼结算。
        """
        if not self._ready or self._repo is None or self._ctx is None:
            return
        uid = str(event.get_sender_id() or "")
        if not uid or uid == self._self_id(event):
            return
        gid = self._group_id(event)
        if gid and not self.cfg.group_enabled(gid):
            return
        now = now_ts()

        # ---- 1) 打工收工 ----
        try:
            me = await self._repo.peek_user(uid)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"peek_user 失败: {exc!r}")
            me = None
        if me is not None and not self._is_work_command(event):
            pay = None
            try:
                async with self._keyed.hold(uid):
                    pay = await work.settle_work(self._ctx, me)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"打工结算失败 uid={uid}: {exc!r}")
            if pay is not None:
                if not pay.owner:
                    pay.owner = str(getattr(me, "name", "") or "")[:16]
                path = await self._render_path(pay)
                if path:
                    yield event.image_result(path)

        # ---- 2) 本人提杆 ----
        if me is None or not me.fish_ready_ts or me.fish_ready_ts > now:
            return
        if not chance(LIFT_CHANCE):
            return
        lifted = None
        try:
            async with self._keyed.hold(uid):
                lifted = await fishing.reel_catch(self._ctx, me)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"提杆结算失败 uid={uid}: {exc!r}")
        if lifted is None:
            return
        if not lifted.owner:
            lifted.owner = str(getattr(me, "name", "") or "")[:16]
        path = await self._render_path(lifted)
        if path:
            yield event.image_result(path)

    # ================================================================ 静态卡片
    def _help_card(self) -> Card:
        c = card(
            "赛博鲸鱼馆 · 指令簿",
            "在深蓝的水里，钓一条属于你的鱼",
            art="help",
            footer="所有指令都会返回一张图喵 | 每日凌晨 4:00 重置",
        )
        c.stats.extend([stat("鲸币", "通用货币"), stat("池塘", "群共享"), stat("鱼护", f"上限{self.cfg.fish_bag_limit}")])
        c.blocks.extend(
            [
                list_block(
                    "🐟 基础",
                    [
                        "签到 —— 基础 50，连续 20 天涨到 100，送随机饵料",
                        "补签 —— 消耗补签卡，补回漏签的一天",
                        "喂食 [白米饭|自动 白米饭] —— 每天早/中/晚 3 次，可设自动购买",
                        "打工 —— 上工后等 1~24 小时，到点后随便发条群消息自动收工，工资=时长×0.32",
                    ],
                ),
                list_block(
                    "🎣 钓鱼",
                    [
                        "钓鱼 [饵料] —— 抛竿等 5~60 分钟（天气修正），到点后自己发消息提杆",
                        "上钩率看池塘/饵料/天气；品质看天气与保底，材料必出",
                        "背包 —— 看鱼护与物品；仓库 —— 按类别查全部物品",
                        "卖鱼 / 卖鱼 杂物 —— 鱼按星级卖鲸币",
                        "池塘 / 捐献 —— 群共享池塘，材料只能钓上来",
                    ],
                ),
                list_block(
                    "💘 社交",
                    [
                        "娶群友 @TA [卡] —— 150 鲸币，有卡自动用卡，10% 起，5 次必中",
                        "离婚 @TA [卡] —— 150 鲸币，有卡自动用卡，50%，2 次必中",
                        "ntr @TA [卡] —— 200 鲸币，有卡自动用卡，5% 起，10 次必中",
                        "我的后宫 / 恩爱榜 / 公共老婆",
                    ],
                ),
                list_block(
                    "🌱 花园",
                    [
                        "养植物 —— 花园帮助与总览（绿色面板）",
                        "播种 [种子] —— 选种子种下，发芽后才揭晓品质",
                        "浇水 / 施肥 氮|磷|钾 / 晒太阳 —— 涨成长与突变",
                        "收获 —— 结果期顶部后收果实 + 一颗种子",
                    ],
                ),
                list_block(
                    "🛒 其他",
                    [
                        "商店 / 购买 红虫 3",
                        "使用 补签卡 | 使用 钟情卡 @TA | 使用 水库一天体验券",
                        "我的 —— 个人面板；仓库 —— 物品资产",
                    ],
                ),
            ]
        )
        return c

    async def _stats_card(self, event: AstrMessageEvent) -> Card:
        gid = self._group_id(event)
        overview: dict[str, Any] = {}
        if self._repo is not None and gid:
            try:
                overview = await self._repo.group_overview(gid)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"读取群概况失败: {exc!r}")
        db_stats = self._db.stats() if self._db is not None else {}
        c = card("鲸鱼馆 · 运行状态", "2核2G 也要优雅地活着", art="pond", tone="legend")
        c.stats.extend(
            [
                stat("本群成婚", overview.get("married", 0)),
                stat("池塘", f"Lv.{overview.get('pond_level', 1)}"),
                stat("缓存", f"{self._renderer.stats().get('entries', 0) if self._renderer else 0} 张"),
            ]
        )
        rows = [
            kv("数据库", str(db_stats.get("path", "-"))),
            kv("连接池", f"{db_stats.get('max_connections', 5)} 条（WAL）"),
            kv("渲染并发", str(self.cfg.render_concurrency)),
            kv("防抖", f"{self.cfg.command_debounce_ms} ms"),
            kv("LLM", "开启" if self.cfg.llm_enabled else "关闭"),
            kv("下次重置", f"{fmt_duration(max(0, seconds_to_reset() // 60 * 60))} 后（当前场次 {day_key()}）"),
        ]
        c.blocks.append(kv_block("⚙️ 内部状态", rows))
        return c