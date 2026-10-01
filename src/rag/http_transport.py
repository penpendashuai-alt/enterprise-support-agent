import asyncio

import httpx

from rag.models import RAGError


async def request(client: httpx.AsyncClient, method: str, url: str, stage: str, **kwargs):
    for attempt in range(3):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt == 2:
                raise RAGError(f"{stage}_unavailable") from None
        else:
            if response.status_code < 400:
                return response
            status = response.status_code
            if status not in {429, 500, 502, 503, 504} or attempt == 2:
                kind = (
                    "auth"
                    if status in {401, 403}
                    else "input"
                    if status == 400
                    else "missing"
                    if status == 404
                    else "unavailable"
                )
                raise RAGError(f"{stage}_{kind}")
        await asyncio.sleep(0.2 * 2**attempt)
    raise RAGError(f"{stage}_unavailable")
