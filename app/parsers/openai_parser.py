"""OpenAI / GPT-5 Mini parser — deliberately **not implemented** in this delivery.

The assessment lists GPT-5 Mini below Gemini in priority, and a parser that cannot be verified
against a real endpoint is worse than an absent one: it would put a provider in the API that
silently fails. So this module declares `IMPLEMENTED = False`, the registry reports it as
disabled with that reason, and any attempt to use it raises a stable error instead of pretending.

Implementing it for real means: verify that the configured model accepts PDF/vision input in the
current `openai` SDK, reuse the single-page splitting strategy from `gemini_parser.py`, return
`PageFormat.MARKDOWN`, and map failures onto TransientError/PermanentError exactly like the
Gemini adapter. Then set `IMPLEMENTED = True`.
"""

from __future__ import annotations

from app.models import ParserId
from app.parsers.base import ParseLimits, ParseResult, Parser, PermanentError

# The registry reads this flag. Flip it only together with a working, tested implementation.
IMPLEMENTED = False


class OpenAIParser(Parser):
    id = ParserId.GPT5_MINI

    def __init__(self, *, api_key: str, model: str, timeout_seconds: float) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    async def parse(self, pdf: bytes, *, filename: str, limits: ParseLimits) -> ParseResult:
        raise PermanentError(
            "The GPT-5 Mini parser is not implemented in this delivery. "
            "Use the pypdf or gemini parser.",
            code="PROVIDER_NOT_IMPLEMENTED",
        )
