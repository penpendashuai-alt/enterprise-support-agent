"""Attach the campaign HTTP meter to real chat and embedding clients."""

import json

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

from resume_support.budget import AttemptLedger, MeteredTransport


class PhysicalObserver(BaseCallbackHandler):
    def __init__(self, campaign, stage, run_id):
        self.campaign, self.stage, self.run_id = campaign, stage, run_id
        self.case_id, self.turn = "setup", -1
        self.attempts = AttemptLedger(
            campaign / "calls/attempts.db",
            json.loads((campaign / "calls/contract.json").read_text(encoding="utf-8")),
        )
        self.blocked = False

    @property
    def exhausted(self):
        return self.blocked or any(e["status"] == "unknown" for e in self.events())

    def events(self):
        return self.attempts.events()

    def context(self):
        return {"run_id": self.run_id, "case_id": self.case_id, "turn": self.turn}

    def client(self, kind):
        observer = self

        class StopAwareTransport(MeteredTransport):
            async def handle_async_request(self, request):
                from resume_support.budget import BudgetStop

                try:
                    return await super().handle_async_request(request)
                except BudgetStop:
                    observer.blocked = True
                    raise

        return httpx.AsyncClient(
            transport=StopAwareTransport(self.attempts, self.stage, kind, self.context),
            timeout=60,
            trust_env=False,
            follow_redirects=False,
        )

    def model(self):
        from core import settings

        if settings.COMPATIBLE_MODEL != "deepseek-v4-pro":
            raise ValueError("Campaign only prices deepseek-v4-pro")
        return ChatOpenAI(
            model=settings.COMPATIBLE_MODEL,
            base_url=settings.COMPATIBLE_BASE_URL,
            api_key=settings.COMPATIBLE_API_KEY,
            temperature=0,
            max_tokens=1800,
            max_retries=0,
            timeout=60,
            stream_usage=True,
            extra_body={"thinking": {"type": "disabled"}},
            http_async_client=self.client("chat"),
        )

    def patch_embeddings(self):
        from rag.embeddings import Embeddings

        original = Embeddings.__init__
        observer = self

        def initialize(instance, settings):
            if settings.CI_TEST_MODE:
                raise ValueError("Real campaign cannot use CI embedding")
            instance.settings = settings
            instance.client = AsyncOpenAI(
                api_key=settings.EMBED_API_KEY.get_secret_value(),
                base_url=settings.EMBED_BASE_URL,
                timeout=settings.EMBED_TIMEOUT,
                max_retries=0,
                http_client=observer.client("embedding"),
            )
            instance.usage_tokens = instance.requests = instance.retries = 0
            instance.elapsed = 0.0

        Embeddings.__init__ = initialize
        return lambda: setattr(Embeddings, "__init__", original)

    def export(self):
        self.attempts.export(self.campaign / "calls/attempts.json")
