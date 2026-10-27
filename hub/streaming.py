from starlette.responses import StreamingResponse


class OwnedStreamingResponse(StreamingResponse):
    """Release upstream connections even if ASGI disconnects before iteration."""
    def __init__(self,*args,finalize,**kwargs):
        super().__init__(*args,**kwargs)
        self.finalize=finalize

    async def __call__(self,scope,receive,send):
        import anyio
        try:
            return await super().__call__(scope,receive,send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.finalize()
