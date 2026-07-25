from urllib.parse import urlparse

import httpx
from tools.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    Tool,
    ToolInvocation,
    ToolKind,
    ToolResult,
    cap_output,
)
from pydantic import BaseModel, Field


class WebFetchParams(BaseModel):
    url: str = Field(..., description="URL to fetch (must be http:// or https://)")
    timeout: int = Field(
        30,
        ge=5,
        le=120,
        description="Request timeout in seconds (default: 120)",
    )


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "Fetch content from a URL. Returns the response body as text"
    kind = ToolKind.NETWORK
    schema = WebFetchParams

    MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = WebFetchParams(**invocation.params)

        parsed = urlparse(params.url)
        if not parsed.scheme or parsed.scheme not in ("http", "https"):
            return ToolResult.error_result(f"Url must be http:// or https://")

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(params.timeout),
                follow_redirects=True,
            ) as client:
                response = await client.get(params.url)
                response.raise_for_status()
                text = response.text
        except httpx.HTTPStatusError as e:
            return ToolResult.error_result(
                f"HTTP {e.response.status_code}: {e.response.reason_phrase}",
            )
        except Exception as e:
            return ToolResult.error_result(f"Request failed: {e}")

        text, truncated = cap_output(
            text,
            hint="Fetch a more specific URL to see the remaining content.",
            max_tokens=self.MAX_OUTPUT_TOKENS,
        )

        return ToolResult.success_result(
            text,
            truncated=truncated,
            metadata={
                "status_code": response.status_code,
                "content_length": len(response.content),
                "truncated": truncated,
            },
        )
