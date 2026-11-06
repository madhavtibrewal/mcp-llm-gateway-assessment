import asyncio

import pytest

from task4_model_router.limiter import RateLimited, TokenRateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(tmp_path, clock):
    return TokenRateLimiter(str(tmp_path / "limits.db"), max_tokens=50_000, window_seconds=60, clock=clock)


def test_admits_up_to_the_limit_then_rejects(limiter):
    for _ in range(5):
        limiter._reserve("t1", 10_000)
    with pytest.raises(RateLimited) as e:
        limiter._reserve("t1", 1)
    assert e.value.retry_after == pytest.approx(60)


def test_the_window_slides(limiter, clock):
    limiter._reserve("t1", 20_000)  # t=0
    clock.now += 30
    limiter._reserve("t1", 20_000)  # t=30
    clock.now += 15  # t=45: 40k used
    with pytest.raises(RateLimited) as e:
        limiter._reserve("t1", 20_000)
    assert e.value.retry_after == pytest.approx(15)  # the t=0 row leaves the window at t=60
    clock.now += 15  # t=60
    limiter._reserve("t1", 20_000)


def test_tenants_have_separate_budgets(limiter):
    limiter._reserve("t1", 50_000)
    limiter._reserve("t2", 50_000)
    with pytest.raises(RateLimited):
        limiter._reserve("t1", 1)


async def test_update_and_release_change_the_usage(limiter):
    row = await limiter.reserve("t1", 40_000)  # estimate
    await limiter.update(row, 1_000)  # what the model actually used
    assert limiter.usage("t1") == 1_000
    await limiter.release(row)
    assert limiter.usage("t1") == 0


def test_state_survives_a_restart(tmp_path, clock):
    path = str(tmp_path / "limits.db")
    TokenRateLimiter(path, clock=clock)._reserve("t1", 30_000)
    assert TokenRateLimiter(path, clock=clock).usage("t1") == 30_000  # it's on disk


async def test_concurrent_requests_never_exceed_the_limit(limiter):
    async def attempt():
        try:
            await limiter.reserve("t1", 1_000)
            return True
        except RateLimited:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(100)))
    assert sum(results) == 50  # exactly 50 x 1000 tokens, never more
