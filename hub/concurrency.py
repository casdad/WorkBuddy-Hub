"""Complete owned work before propagating cancellation to its caller."""
import asyncio
import anyio
from functools import wraps


def finish_owned(function):
    @wraps(function)
    async def wrapped(*args,**kwargs):
        return await complete(function(*args,**kwargs))
    return wrapped


def serialized_admission(function):
    @wraps(function)
    async def wrapped(self,*args,**kwargs):
        async with self.admission_lock:
            return await function(self,*args,**kwargs)
    return wrapped


async def complete(awaitable):
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    # Shield ASGI cancellation scopes as well as direct asyncio Task.cancel().
    with anyio.CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result
