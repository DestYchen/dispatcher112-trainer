import asyncio

import httpx
import pytest

from app.scoring import grammar


async def test_concurrent_identical_checks_share_request_but_outage_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    available = True
    release = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await release.wait()
        if not available:
            raise httpx.ConnectError("Unavailable", request=request)
        return httpx.Response(200, json={"matches": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(grammar, "shared_client", client)
        tasks = [asyncio.create_task(grammar.check_grammar("Одинаковый доклад")) for _ in range(20)]
        for _ in range(10):
            if calls:
                break
            await asyncio.sleep(0)
        assert calls == 1
        release.set()
        results = await asyncio.gather(*tasks)
        assert all(item.available for item in results)
        assert not grammar.pending_checks
        available = False
        result = await grammar.check_grammar("Одинаковый доклад")
        assert not result.available and calls == 2
