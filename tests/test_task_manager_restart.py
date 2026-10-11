import asyncio

from core.task_manager import TaskManager


def test_manual_restart_preserves_autorestart_setting():
    async def scenario():
        manager = TaskManager()
        calls = 0

        async def worker():
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()
            raise RuntimeError("stop after manual restart")

        await manager.create("one-shot", worker, autorestart=False)
        await asyncio.sleep(0)
        try:
            await manager.restart_registered("one-shot")
            await asyncio.sleep(0.05)
            assert calls == 2
            assert manager.stats()["one-shot"] == "error: RuntimeError"
        finally:
            await manager.cancel_all(timeout=1)

    asyncio.run(scenario())
