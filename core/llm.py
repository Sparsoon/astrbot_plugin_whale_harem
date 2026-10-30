"""LLM 桥接：喂食时让模型说一句人话，并且**永远不能把机器人卡住**。

三道保险：
    1. 只调用 AstrBot 当前会话的 provider，不自己 new 客户端；
    2. asyncio.wait_for 硬超时（默认 12s），超时立即回退本地语料；
    3. 任何异常（没配置模型 / 额度用尽 / 网络断）都吞掉并返回 None。
"""

from __future__ import annotations

import asyncio
import random

from .config import PluginConfig
from .log import log

FALLBACK_LINES: tuple[str, ...] = (
    "嚼嚼……这米饭有点东西，再来一碗！",
    "你就这么看着我吃饭吗？真是的……",
    "嗝。本鲸吃饱了，现在心情很好。",
    "这盆饭，比昨天的更香一点点。",
    "吃人嘴软，我决定今天对你好一点。",
    "别急，我还没咽下去呢。",
    "你是不是把盐当成糖了……算了，我吃。",
    "再来点，我还能吃。",
)


class LLMClient:
    """把 AstrBot 的 LLM 调用包装成"可失败、可超时、可降级"的小工具。"""

    __slots__ = ("_context", "_cfg", "_fallback_rng")

    def __init__(self, context: object | None, cfg: PluginConfig) -> None:
        self._context = context
        self._cfg = cfg
        self._fallback_rng = random.Random()

    async def chat(self, prompt: str, umo: str = "", timeout: float | None = None) -> str | None:
        """返回一句模型生成的话；失败返回 None（调用方自行兜底）。"""
        if not self._cfg.llm_enabled or self._context is None:
            return None
        limit = float(timeout or self._cfg.llm_timeout_sec)
        try:
            return await asyncio.wait_for(self._generate(prompt, umo), timeout=limit)
        except asyncio.TimeoutError:
            log.warning("LLM 调用超时，使用本地语料")
            return None
        except Exception as exc:  # noqa: BLE001 - 绝不能因为模型问题中断玩法
            log.warning(f"LLM 调用失败: {exc!r}")
            return None

    async def _generate(self, prompt: str, umo: str) -> str | None:
        ctx = self._context
        if ctx is None:
            return None
        provider_id = None
        # AstrBot >= 4.5.7 新接口
        get_pid = getattr(ctx, "get_current_chat_provider_id", None)
        if callable(get_pid):
            try:
                provider_id = await get_pid(umo=umo)
            except Exception:  # noqa: BLE001
                provider_id = None
        generate = getattr(ctx, "llm_generate", None)
        if callable(generate):
            resp = await generate(chat_provider_id=provider_id, prompt=prompt)
            text = getattr(resp, "completion_text", None)
            if text:
                return self._clean(str(text))
        # 老接口兜底
        get_provider = getattr(ctx, "get_using_provider", None)
        if callable(get_provider):
            provider = get_provider(umo=umo) if _accepts_umo(get_provider) else get_provider()
            if provider is not None:
                resp = await provider.text_chat(prompt=prompt, session_id=umo or None)
                text = getattr(resp, "completion_text", None)
                if text:
                    return self._clean(str(text))
        return None

    @staticmethod
    def _clean(text: str) -> str | None:
        text = text.strip().strip('"').strip("“”").replace("\n", " ")
        if not text:
            return None
        return text[:60]

    def fallback(self) -> str:
        return self._fallback_rng.choice(FALLBACK_LINES)


def _accepts_umo(fn: object) -> bool:
    try:
        import inspect

        return "umo" in inspect.signature(fn).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False