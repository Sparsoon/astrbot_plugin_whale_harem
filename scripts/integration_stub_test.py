#!/usr/bin/env python3
"""端到端集成测试（不需要真的 AstrBot / QQ）。

做法：在临时目录里生成一个**最小 astrbot stub 包**（只实现 main.py 用到的那 6 个符号），
然后把插件当普通包导入，构造假事件，直接调用 21 条指令的处理函数，验证：

    1. 指令注册、plugin 初始化、DB/渲染器装配
    2. `/签到` 等指令返回 image_result 且 PNG 真实存在（不是降级文字）
    3. @ 解析（娶群友 / 喂食 / 使用 钟情卡）
    4. 防抖（1.5s 内第二次直接 None）
    5. 群白名单（不在名单 -> None）
    6. 业务异常 -> 错误卡图片（不是崩溃）
    7. terminate() 正常释放

用法：
    python scripts/integration_stub_test.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import textwrap
import traceback
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("WHALE_FEED_ANYTIME", "1")

ROOT = Path(__file__).resolve().parents[1]
_FAILS: list[str] = []

STUB_FILES = {
    "astrbot/__init__.py": """
        from . import api
        __version__ = "stub-4.9"
    """,
    "astrbot/api/__init__.py": """
        import logging
        logger = logging.getLogger("astrbot.stub")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("[stub] %(levelname)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
    """,
    "astrbot/api/event.py": """
        from types import SimpleNamespace

        class MessageEventResult:
            def __init__(self, kind, value):
                self.kind = kind
                self.value = value

            def __repr__(self):
                return f"<Result {self.kind} {self.value!r}>"


        class AstrMessageEvent:
            def __init__(self, sender_id="10001", sender_name="测试群友", group_id="30003",
                         text="", chain=None, self_id="999"):
                self._sender_id = sender_id
                self._sender_name = sender_name
                self._group_id = group_id
                self.message_str = text
                self.unified_msg_origin = (
                    f"aiocqhttp:GroupMessage:{group_id}" if group_id else f"aiocqhttp:FriendMessage:{sender_id}"
                )
                self.message_obj = SimpleNamespace(
                    message=list(chain or []), self_id=self_id, group_id=group_id
                )

            # --- 插件用到的访问器 ---
            def get_sender_id(self):
                return self._sender_id

            def get_sender_name(self):
                return self._sender_name

            def get_group_id(self):
                return self._group_id

            def image_result(self, path):
                return MessageEventResult("image", path)

            def plain_result(self, text):
                return MessageEventResult("text", text)


        class _EventMessageType:
            GROUP_MESSAGE = "group"
            PRIVATE_MESSAGE = "private"
            OTHER_MESSAGE = "other"
            ALL = "all"


        class _Filter:
            EventMessageType = _EventMessageType

            def command(self, name, alias=None, **kwargs):
                def deco(fn):
                    fn._command = name
                    fn._alias = set(alias or ())
                    return fn
                return deco

            def event_message_type(self, event_message_type, **kwargs):
                def deco(fn):
                    fn._event_type = event_message_type
                    return fn
                return deco

            def llm_tool(self, *args, **kwargs):
                def deco(fn):
                    return fn
                return deco


        filter = _Filter()
    """,
    "astrbot/api/star.py": """
        class Context:
            def __init__(self):
                self.provider_id = "stub-provider"

            async def get_current_chat_provider_id(self, umo=None):
                return self.provider_id

            async def llm_generate(self, chat_provider_id=None, prompt="", **kwargs):
                return SimpleNamespace(completion_text="（stub 模型台词）今天这碗饭勉强算你有心。")

            async def get_using_provider(self, umo=None):
                return None


        class Star:
            def __init__(self, context):
                self.context = context

            async def initialize(self):
                return None

            async def terminate(self):
                return None


        def register(name, author, desc, version, **kwargs):
            def deco(cls):
                cls._plugin_name = name
                cls._plugin_author = author
                cls._plugin_version = version
                return cls
            return deco
    """,
    "astrbot/api/message_components.py": """
        class At:
            def __init__(self, qq=None, name=""):
                self.qq = qq
                self.name = name


        class Plain:
            def __init__(self, text=""):
                self.text = text


        class Image:
            @classmethod
            def fromFileSystem(cls, path):
                return cls(path)
    """,
}


def _write_stub(base: Path) -> None:
    for rel, content in STUB_FILES.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _bootstrap_package() -> str:
    parent = str(ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    names: list[str] = []
    for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            names.append(line.split(":", 1)[1].strip().strip("'\""))
            break
    names.extend([ROOT.name, "astrbot_plugin_whale_harem"])
    for name in names:
        if not name or not name.isidentifier():
            continue
        try:
            importlib.import_module(f"{name}.main")
            return name
        except ImportError as exc:
            last = exc
    raise SystemExit(f"无法导入插件 main.py：{last!r}")


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ok]   {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        _FAILS.append(f"{name} {detail}")


async def _call(plugin, command: str, event):
    """按指令名找到 handler 并调用（stub 的 filter 只打标记，这里用标记反查）。"""
    for attr in dir(plugin):
        fn = getattr(plugin, attr, None)
        if callable(fn) and getattr(fn, "_command", None) == command:
            return await fn(event)
    raise AssertionError(f"没有注册指令 {command}")


def _is_image(result) -> bool:
    return result is not None and getattr(result, "kind", "") == "image" and Path(result.value).is_file()


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="whale_it_"))
    stub_root = tmp / "stub"
    _write_stub(stub_root)
    sys.path.insert(0, str(stub_root))

    import os

    os.chdir(tmp)  # 让 plugin_data / 缓存都落在临时目录
    try:
        from astrbot.api.event import AstrMessageEvent  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print("stub astrbot 导入失败:", exc)
        return 2

    pkg = _bootstrap_package()
    main_mod = importlib.import_module(f"{pkg}.main")
    star_mod = importlib.import_module("astrbot.api.star")
    comp = importlib.import_module("astrbot.api.message_components")

    plugin = main_mod.WhaleHaremPlugin(
        star_mod.Context(),
        {"db_path": str(tmp / "it.db"), "command_debounce_ms": 1500, "llm_enabled": False},
    )
    await plugin.initialize()
    check("插件初始化", plugin._ready)
    # 给测试用户一点饵料/米饭，后面要走真实的玩法链路
    await plugin._repo.add_items("10001", {"bait_normal": 8, "rice_bowl": 3})

    try:
        gid = "30003"
        uid = "10001"

        def ev(text: str = "", chain=None, group_id: str = gid, sender: str = uid):
            return AstrMessageEvent(
                sender_id=sender,
                sender_name="测试群友",
                group_id=group_id,
                text=text,
                chain=chain or [],
            )

        async def call(command: str, event):
            """每条独立用例前清空防抖状态，避免用例互相干扰（防抖本身单独测）。"""
            plugin._debouncer.clear()
            return await _call(plugin, command, event)

        # ---------------------------------------------------------- 基础指令
        print("\n[1] 指令 -> 图片")
        r = await call("签到", ev("签到"))
        check("签到返回图片", _is_image(r), repr(r))

        r = await call("帮助", ev("帮助"))
        check("帮助返回图片", _is_image(r), repr(r))

        r = await call("我的", ev("我的"))
        check("我的返回图片", _is_image(r), repr(r))

        # ---------------------------------------------------------- 防抖
        plugin._debouncer.clear()
        r1 = await _call(plugin, "我的", ev("我的"))
        r2 = await _call(plugin, "我的", ev("我的"))
        check("1.5s 内第二次被防抖丢弃", r1 is not None and r2 is None, f"{r1!r} / {r2!r}")

        # ---------------------------------------------------------- 群白名单
        plugin.cfg.enabled_groups = ["999999"]
        plugin._debouncer.clear()
        r = await call("签到", ev("签到"))
        check("群不在白名单 -> None", r is None, repr(r))
        plugin.cfg.enabled_groups = []

        # 防抖键是 uid，换个人测试；同时清掉 debounce 状态
        plugin._debouncer.clear()

        # ---------------------------------------------------------- @ 解析
        print("\n[2] @ 解析 / 业务链")
        at = comp.At(qq="20002", name="测试老婆")
        r = await call("喂食", ev("喂食", chain=[at]))
        check("喂食 @目标 返回图片", _is_image(r), repr(r))

        r = await call("娶群友", ev("娶群友", chain=[comp.At(qq="20003", name="隔壁小红")]))
        check("娶群友 返回图片", _is_image(r), repr(r))

        # 钟情卡需要 @
        await plugin._repo.add_items(uid, {"card_loyal": 1})
        plugin._debouncer.clear()
        r = await call("使用", ev("使用 钟情卡", chain=[at]))
        check("使用 钟情卡 @TA 返回图片（此前是 P0）", _is_image(r), repr(r))

        # 体验券（无 @ 也应能用）
        await plugin._repo.add_items(uid, {"ticket_reservoir": 1})
        plugin._debouncer.clear()
        r = await call("使用", ev("使用 水库一天体验券"))
        check("使用 体验券 无需 @", _is_image(r), repr(r))

        # ---------------------------------------------------------- 群聊触发
        print("\n[2b] 群聊触发（咬钩 / 收工）")
        plugin._debouncer.clear()
        r = await call("钓鱼", ev("钓鱼 普通饵料"))
        check("抛竿返回图片", _is_image(r), repr(r))
        await plugin._repo.update_user(uid, fish_ready_ts=1)
        bitten = False
        for _ in range(40):
            results = [item async for item in plugin.on_group_activity(ev("群里随便聊两句"))]
            if results:
                bitten = True
                check("群聊触发咬钩返回图片", _is_image(results[0]), repr(results))
                break
        check("40 条消息内触发咬钩", bitten)

        plugin._debouncer.clear()
        r = await call("打工", ev("打工"))
        check("上工返回图片", _is_image(r), repr(r))
        await plugin._repo.update_user(uid, work_ready_ts=1)
        paid = False
        for _ in range(3):
            results = [item async for item in plugin.on_group_activity(ev("我下班了"))]
            if results:
                paid = True
                check("群聊触发收工返回图片", _is_image(results[0]), repr(results))
                break
        check("发消息触发收工", paid)

        # ---------------------------------------------------------- 错误卡
        print("\n[3] 异常兜底")
        plugin._debouncer.clear()
        r = await call("使用", ev("使用 不存在的东西"))
        check("未知道具 -> 错误卡图片", _is_image(r), repr(r))

        plugin._debouncer.clear()
        r = await call("购买", ev("购买 娶群友卡 1"))
        check("买非卖品 -> 错误卡图片", _is_image(r), repr(r))

        plugin._debouncer.clear()
        r = await call("娶群友", ev("娶群友"))
        check("没 @ 的娶群友 -> 错误卡图片", _is_image(r), repr(r))

        # 私聊
        plugin._debouncer.clear()
        r = await call("签到", ev("签到", group_id=""))
        check("私聊 -> 提示卡（仍返回结果）", r is not None, repr(r))

        # ---------------------------------------------------------- 钓鱼 / 商店 / 排名
        print("\n[4] 其余指令冒烟")
        for cmd, text in [
            ("钓鱼", "钓鱼 普通饵料"),
            ("背包", "背包"),
            ("池塘", "池塘"),
            ("商店", "商店"),
            ("我的后宫", "我的后宫"),
            ("恩爱榜", "恩爱榜"),
            ("公共老婆", "公共老婆"),
            ("打工", "打工"),
            ("鲸鱼馆", "鲸鱼馆"),
        ]:
            plugin._debouncer.clear()
            r = await call(cmd, ev(text))
            check(f"{cmd} 返回图片", _is_image(r), repr(r))

        # ---------------------------------------------------------- 收尾
        print("\n[5] 资源释放")
        stats = plugin._renderer.stats() if plugin._renderer else {}
        print(f"  渲染统计: {stats}")
        cache_dir = Path(getattr(plugin._renderer, "_cache_dir", tmp))
        await plugin.terminate()
        check("terminate 后 ready=False", plugin._ready is False)
        left = list(cache_dir.glob("*.png")) if cache_dir.is_dir() else []
        check("关闭后缓存目录已清空", len(left) == 0, f"剩 {len(left)} 个文件")

    finally:
        if plugin._ready:
            await plugin.terminate()
    print("\n" + "=" * 52)
    if _FAILS:
        print(f"失败 {len(_FAILS)} 项：")
        for item in _FAILS:
            print("  -", item)
        return 1
    print("集成测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(2)