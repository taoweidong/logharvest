"""登录限流：按"用户名 + 客户端 IP"做滑动窗口计数。

``/api/login`` 是控制面唯一匿名可达、且每次请求都要跑一次 bcrypt 校验（约 100ms
量级）的接口。不限流等于同时对外开放两样东西：凭据爆破，以及一个廉价的 CPU 耗尽
放大器。规格没有为控制面规定限流，这里按最小实现补上。

设计取舍（每条都是有意为之，改动前请先读）：

- **只统计失败尝试**。成功登录会清空该 key 的窗口，所以知道口令的人不会被自己之
  前的失败挡在门外。代价是攻击者确实能把"某个用户名 + 他自己那个 IP"锁住；锁定
  粒度是 ``(username, ip)``，来自其他来源的同一用户不受影响。
- **不读 ``X-Forwarded-For``**。在没有可信代理清单的前提下信任它，等于给攻击者一个
  "换个头就换一个 key"的绕过后门；这里只认 ``request.client.host``，即 TCP 对端
  （经反向代理时就是代理自己，语义依然成立）。
- **进程内计数，不引入 Redis**。当前部署是单副本。多副本时每个副本各自计数，实际
  放行量是阈值 × 副本数，届时要么按副本数折算阈值，要么换成共享计数。
- **key 数量必须有上界**。用海量随机用户名可以撑爆计数表，这是内存限流器经典的
  放大面；超过 ``max_keys`` 时淘汰最久未活动的 key。
"""

from __future__ import annotations

import math
from collections import OrderedDict, deque
from collections.abc import Callable
from time import monotonic

DEFAULT_MAX_KEYS = 10_000


def attempt_key(username: str, client_host: str | None) -> str:
    """限流的计数维度：用户名 + 来源地址。

    ``client_host`` 为 ``None``（取不到对端，例如某些测试通道）时退化为仅按用户名
    计数：宁可从严，也不要因为拿不到地址就完全不限流。
    """
    normalized = username.strip().lower()
    return f"{normalized}|{client_host or '-'}"


class SlidingWindowLimiter:
    """每个 key 在一个窗口内最多允许 ``max_attempts`` 次失败尝试。"""

    def __init__(
        self,
        max_attempts: int,
        window_seconds: float,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_attempts < 0:
            raise ValueError("max_attempts must be >= 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        # 用户名 -> 失败时刻（升序）。OrderedDict 的插入顺序即"最近使用"顺序。
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str) -> bool:
        """是否还允许再试一次；只读判定，不记账（便于测试与"先判后记"的顺序）。"""
        return len(self._recent(key)) < self._max_attempts

    def record_failure(self, key: str) -> None:
        """记一次失败尝试；重复 key 会挪到队尾，让淘汰顺序等同于 LRU。"""
        events = self._failures.get(key)
        if events is None:
            self._failures[key] = deque([self._clock()])
        else:
            events.append(self._clock())
            self._failures.move_to_end(key)
        self._evict_if_needed()

    def reset(self, key: str) -> None:
        """成功登录：这个 key 的失败记录不再有意义，直接清掉。"""
        self._failures.pop(key, None)

    def retry_after(self, key: str) -> int:
        """还要等多少秒才可能再放行（向上取整），用于 ``Retry-After`` 响应头。"""
        recent = self._recent(key)
        if len(recent) < self._max_attempts:
            return 0
        return max(1, math.ceil(recent[0] + self._window - self._clock()))

    def _recent(self, key: str) -> list[float]:
        """窗口内仍然有效的失败时刻（升序）。``check`` 需要的是"有效"而不是"存过"。"""
        events = self._failures.get(key)
        if events is None:
            return []
        cutoff = self._clock() - self._window
        return [stamp for stamp in events if stamp > cutoff]

    def _evict_if_needed(self) -> None:
        while len(self._failures) > self._max_keys:
            self._failures.popitem(last=False)
