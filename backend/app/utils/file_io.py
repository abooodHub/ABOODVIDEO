"""Bounded blocking file work, kept away from the ASGI event loop."""
from functools import partial
import errno
import anyio

_limiter = anyio.CapacityLimiter(4)
_CAPACITY_ERRNOS = {getattr(errno, name) for name in ("ENOSPC", "EDQUOT") if hasattr(errno, name)}


def is_storage_capacity_error(error: BaseException) -> bool:
    """Distinguish retryable filesystem capacity/quota exhaustion from other I/O faults."""
    return isinstance(error, OSError) and error.errno in _CAPACITY_ERRNOS


async def file_io(function, *args, **kwargs):
    return await anyio.to_thread.run_sync(partial(function, *args, **kwargs), limiter=_limiter)
