import pytest

from src.domain.services.rate_limit import SlidingWindowLimiter, attempt_key


class FakeClock:
    """可控时钟：滑动窗口的语义必须能被精确推进，不能靠 sleep。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_limiter(max_attempts: int = 3, window: float = 60.0, **overrides) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(
        max_attempts=max_attempts, window_seconds=window, clock=FakeClock(), **overrides
    )


def test_failures_up_to_the_limit_are_allowed() -> None:
    limiter = make_limiter()
    for _ in range(3):
        assert limiter.check("admin|10.0.0.1") is True
        limiter.record_failure("admin|10.0.0.1")

    assert limiter.check("admin|10.0.0.1") is False


def test_expired_failures_leave_the_window() -> None:
    """窗口是滑动的：过了 window 秒，失败记录自己失效，不需要任何清理动作。"""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(2, 60.0, clock=clock)
    for _ in range(2):
        limiter.record_failure("admin|10.0.0.1")
    assert limiter.check("admin|10.0.0.1") is False

    clock.advance(61)
    assert limiter.check("admin|10.0.0.1") is True


def test_success_resets_the_key_so_a_known_password_never_locks_itself_out() -> None:
    limiter = make_limiter()
    for _ in range(2):
        limiter.record_failure("admin|10.0.0.1")

    limiter.reset("admin|10.0.0.1")

    assert limiter.check("admin|10.0.0.1") is True


def test_limit_is_scoped_to_username_and_source_address() -> None:
    """锁定粒度是 (用户名, 来源)：换一个来源的同名用户不该被别人拖累。"""
    limiter = make_limiter(1)
    limiter.record_failure("admin|10.0.0.1")

    assert limiter.check("admin|10.0.0.1") is False
    assert limiter.check("admin|10.0.0.2") is True
    assert limiter.check("other|10.0.0.1") is True


def test_tracked_keys_are_bounded_by_lru_eviction() -> None:
    """用海量随机用户名撑爆计数表是内存限流器的经典放大面。"""
    limiter = make_limiter(1, max_keys=2)
    limiter.record_failure("a|1")
    limiter.record_failure("b|1")
    limiter.record_failure("c|1")

    # a 是最久未活动的，被淘汰后它的计数也随之消失（从严的新窗口，而非放行记录）。
    assert limiter.check("a|1") is True
    assert limiter.check("b|1") is False
    assert limiter.check("c|1") is False


def test_repeats_move_the_key_to_the_end_of_the_eviction_order() -> None:
    limiter = make_limiter(1, max_keys=2)
    limiter.record_failure("a|1")
    limiter.record_failure("b|1")
    limiter.record_failure("a|1")  # a 变成最近使用，b 成了最久未活动

    limiter.record_failure("c|1")

    assert limiter.check("a|1") is False
    assert limiter.check("b|1") is True


def test_retry_after_reports_seconds_until_the_oldest_failure_expires() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(2, 60.0, clock=clock)
    limiter.record_failure("admin|10.0.0.1")
    clock.advance(10)
    limiter.record_failure("admin|10.0.0.1")

    # 最早一次失败在 t=1000，窗口 60 秒；现在是 t=1010。
    assert limiter.retry_after("admin|10.0.0.1") == 50
    clock.advance(50)
    assert limiter.retry_after("admin|10.0.0.1") == 0


def test_retry_after_is_zero_before_the_limit_is_reached() -> None:
    limiter = make_limiter()
    limiter.record_failure("admin|10.0.0.1")

    assert limiter.retry_after("admin|10.0.0.1") == 0


def test_zero_attempts_blocks_every_login() -> None:
    """阈值 0 是运维可用的"临时关门"，必须真的关得住。"""
    limiter = make_limiter(max_attempts=0)

    assert limiter.check("admin|10.0.0.1") is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_attempts": -1}, "max_attempts"),
        ({"window_seconds": 0}, "window_seconds"),
        ({"max_keys": 0}, "max_keys"),
    ],
)
def test_limiter_rejects_nonsense_configuration(overrides: dict, message: str) -> None:
    kwargs: dict = {"max_attempts": 3, "window_seconds": 60.0}
    kwargs.update(overrides)
    with pytest.raises(ValueError, match=message):
        SlidingWindowLimiter(**kwargs)


def test_attempt_key_normalizes_the_username_and_keeps_the_host() -> None:
    assert attempt_key("Admin", "10.0.0.1") == "admin|10.0.0.1"
    assert attempt_key(" admin ", "10.0.0.1") == "admin|10.0.0.1"
    assert attempt_key("admin", "10.0.0.1") != attempt_key("admin", "10.0.0.2")


def test_attempt_key_falls_back_to_username_only_without_a_host() -> None:
    """拿不到对端地址时从严（只按用户名），而不是干脆不限流。"""
    assert attempt_key("admin", None) == "admin|-"
