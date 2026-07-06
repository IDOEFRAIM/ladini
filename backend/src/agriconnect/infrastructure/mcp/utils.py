from __future__ import annotations

import asyncio
from asyncio import Runner
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine, Optional, TypeVar

_T = TypeVar("_T")


def _run_with_runner(coro: Coroutine[Any, Any, _T]) -> _T:
    with Runner() as runner:
        return runner.run(coro)


def run_coro_blocking(coro: Coroutine[Any, Any, _T], *, timeout: Optional[float] = None) -> _T:
    """Execute an async coroutine from sync code without nesting event loops."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_with_runner(coro)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_with_runner, coro)
        return future.result(timeout=timeout)
