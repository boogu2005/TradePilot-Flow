"""Run blocking SQLAlchemy work outside the trading event loop."""
import asyncio


async def repository_call(repository, method, *args):
    function = getattr(repository, method)
    if getattr(repository, "blocking_io", False):
        return await asyncio.to_thread(function, *args)
    return function(*args)
