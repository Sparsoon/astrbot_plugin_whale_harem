"""随机工具 —— O(1) 权重抽样（Alias Method / Vose）。

为什么不用 `random.choices(weights=...)`？
    它在每次调用时都要 O(n) 构建累积权重并二分，鱼获池有几十项、道具池有十几项，
    高峰期（群里几十人连抽）会白白消耗 2 核 CPU。Alias 表在进程启动时构建一次，
    之后每次抽样固定 O(1)：一次随机下标 + 一次比较。

用法：
    pool = AliasSampler({"a": 50, "b": 25, "c": 25})
    pool.pick()            # 概率 0.5 / 0.25 / 0.25
"""

from __future__ import annotations

import random
from typing import Any, Iterable, Mapping, Sequence

_DEFAULT_RNG = random.Random()


def rng() -> random.Random:
    return _DEFAULT_RNG


def seed(value: int | None = None) -> None:
    _DEFAULT_RNG.seed(value)


class AliasSampler:
    """O(1) 离散分布采样器（不可变，构建后线程安全）。"""

    __slots__ = ("_keys", "_prob", "_alias", "_n")

    def __init__(self, weights: Mapping[Any, float] | Sequence[tuple[Any, float]]) -> None:
        items: list[tuple[Any, float]] = (
            list(weights.items()) if isinstance(weights, Mapping) else list(weights)
        )
        cleaned = [(k, float(w)) for k, w in items if w is not None and float(w) > 0]
        if not cleaned:
            raise ValueError("AliasSampler 需要至少一个正权重")
        total = sum(w for _, w in cleaned)
        n = len(cleaned)
        scaled = [(k, w * n / total) for k, w in cleaned]

        prob = [0.0] * n
        alias = [0] * n
        small: list[int] = []
        large: list[int] = []
        for idx, (_, p) in enumerate(scaled):
            prob[idx] = p
            if p < 1.0:
                small.append(idx)
            else:
                large.append(idx)

        while small and large:
            s = small.pop()
            l = large.pop()
            alias[s] = l
            prob[l] = prob[l] + prob[s] - 1.0
            if prob[l] < 1.0:
                small.append(l)
            else:
                large.append(l)
        for idx in large:
            prob[idx] = 1.0
        for idx in small:
            prob[idx] = 1.0

        self._keys = [k for k, _ in cleaned]
        self._prob = prob
        self._alias = alias
        self._n = n

    def __len__(self) -> int:
        return self._n

    def pick(self, r: random.Random | None = None) -> Any:
        r = r or _DEFAULT_RNG
        idx = int(r.random() * self._n)
        if idx >= self._n:  # 浮点边界保护
            idx = self._n - 1
        if r.random() < self._prob[idx]:
            return self._keys[idx]
        return self._keys[self._alias[idx]]


def pick_weighted(weights: Mapping[Any, float], r: random.Random | None = None) -> Any:
    """小规模场景的 O(n) 兜底抽样（n < 8 时比建表更划算）。"""
    r = r or _DEFAULT_RNG
    total = 0.0
    for w in weights.values():
        if w and w > 0:
            total += w
    if total <= 0:
        raise ValueError("权重之和必须为正")
    point = r.random() * total
    upto = 0.0
    last = None
    for key, w in weights.items():
        if not w or w <= 0:
            continue
        upto += w
        last = key
        if point < upto:
            return key
    return last


def chance(p: float, r: random.Random | None = None) -> bool:
    r = r or _DEFAULT_RNG
    if p <= 0:
        return False
    if p >= 1:
        return True
    return r.random() < p


def randint(a: int, b: int, r: random.Random | None = None) -> int:
    r = r or _DEFAULT_RNG
    if b < a:
        a, b = b, a
    return r.randint(a, b)


def uniform(a: float, b: float, r: random.Random | None = None) -> float:
    r = r or _DEFAULT_RNG
    if b < a:
        a, b = b, a
    return r.uniform(a, b)


def choice(seq: Sequence[Any], r: random.Random | None = None) -> Any:
    r = r or _DEFAULT_RNG
    if not seq:
        raise ValueError("空序列无法抽样")
    return seq[r.randrange(len(seq))]


def sample_unique(pool: Sequence[Any], k: int, r: random.Random | None = None) -> list[Any]:
    r = r or _DEFAULT_RNG
    pool = list(pool)
    if k >= len(pool):
        r.shuffle(pool)
        return pool
    return r.sample(pool, k)


def build_table(entries: Iterable[tuple[Any, float]]) -> AliasSampler:
    """从 (key, weight) 迭代器构建 O(1) 采样表。"""
    return AliasSampler(list(entries))