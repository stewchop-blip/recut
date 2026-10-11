"""Reap bounded media subprocesses before cancellation/timeout cleanup."""
import asyncio


async def communicate(proc, timeout):
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
        raise
