"""Small replaceable provider adapter; no client or key needed when disabled."""

from __future__ import annotations

from typing import Any, Protocol

from openai import AsyncOpenAI


class AssistantProvider(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class OpenAIProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self.client = AsyncOpenAI(api_key=api_key, timeout=45, max_retries=1)
        self.model = model

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            max_completion_tokens=1800,
        )
        if not response.choices:
            raise RuntimeError("Provider returned no assistant response")
        return response.choices[0].message.model_dump(exclude_none=True)

    async def close(self) -> None:
        await self.client.close()
