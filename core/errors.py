"""异常定义：所有业务异常都要可被指令层捕获并转成一张友好图片。"""

from __future__ import annotations

from typing import Iterable


class WhaleError(Exception):
    """业务异常基类。message 直接展示；hint 写"下一步可以做什么"。"""

    def __init__(
        self,
        message: str,
        *,
        hint: str = "",
        commands: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.commands = tuple(str(c) for c in commands if str(c).strip())


class ConfigError(WhaleError):
    pass


class DBPoolTimeout(WhaleError):
    def __init__(self, message: str = "数据库正忙，请 5 秒后再试") -> None:
        super().__init__(
            message,
            hint="这通常是瞬时高峰；连续失败请找管理员看日志",
            commands=["/鲸鱼馆"],
        )


class NotEnoughCoins(WhaleError):
    def __init__(self, need: int, have: int) -> None:
        super().__init__(
            f"鲸币不够：需要 {need}，你现在有 {have}",
            hint="去 /签到、/打工，或者把鱼护里的鱼 /卖鱼 换钱",
            commands=["/签到", "/打工", "/卖鱼"],
        )


class NotEnoughItem(WhaleError):
    def __init__(self, item_name: str, need: int = 1, *, hint: str = "") -> None:
        super().__init__(
            f"背包里没有足够的「{item_name}」x{need}",
            hint=hint or "可以用 /商店 看看有没有卖，或 /钓鱼、/签到 碰碰运气",
            commands=["/商店", "/钓鱼", "/签到"],
        )


class NotInGroup(WhaleError):
    def __init__(self) -> None:
        super().__init__(
            "这个玩法只能在一个群里玩",
            hint="把机器人拉进群，或在群聊里 @它 再发指令",
            commands=["/帮助"],
        )


class TargetMissing(WhaleError):
    def __init__(self) -> None:
        super().__init__(
            "要先 @ 一位群友",
            hint="在群里输入指令后，从成员列表里点一下对方的头像",
            commands=["/娶群友 @TA", "/喂食 @TA"],
        )


class RenderError(WhaleError):
    pass


class AlreadyOwned(WhaleError):
    def __init__(self, owner_label: str) -> None:
        super().__init__(
            f"对方已经有主了（{owner_label}）",
            hint="可以 /ntr @TA 抢一次，或者换个人追",
            commands=["/ntr @TA", "/公共老婆"],
        )
