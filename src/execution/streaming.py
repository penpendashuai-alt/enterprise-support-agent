from starlette.responses import StreamingResponse


class SupportStreamingResponse(StreamingResponse):
    """The support middleware owns disconnect monitoring and cancellation."""

    async def __call__(self, scope, receive, send):
        try:
            await self.stream_response(send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close:
                await close()
        if self.background is not None:
            await self.background()
