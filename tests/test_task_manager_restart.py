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


def test_wait_timeout_does_not_cancel_managed_task():
    async def scenario():
        manager = TaskManager()
        started = asyncio.Event()
        release = asyncio.Event()

        async def worker():
            started.set()
            await release.wait()

        task = await manager.create("long-running", worker, autorestart=False)
        await started.wait()
        try:
            await manager.wait("long-running", timeout=0.01)

            assert manager.is_running("long-running")
            assert not task.cancelled()

            release.set()
            await manager.wait("long-running", timeout=1)
            assert task.done()
        finally:
            await manager.cancel_all(timeout=1)

    asyncio.run(scenario())
