"""Settings and the paid/optional steps of the read cascade.

Everything comes from the environment, so the same image runs with or without a
neuraldeep key:

  NEURALDEEP_API_KEY     enables the remote reader step (/v1/search/read, paid per
                         call) and is the default key for the gate's LLM judge;
  NEURALDEEP_BASE_URL    default https://api.neuraldeep.ru;
  JUDGE_LLM_URL/KEY/MODEL judge endpoint (OpenAI-compatible), defaults to
                         NEURALDEEP_BASE_URL/v1 and qwen3.6-35b-a3b-noreason;
  JUDGE_TIMEOUT_S        default 4; the judge failing means "not ok" (go on);
  PROXY_URL              egress proxy for foreign sites that refused us;
  PLAYWRIGHT_URL         optional browser service (POST /read {url, use_proxy}).

Without a key the cascade is fast path only: the gate still rejects stubs and the
caller gets an honest 422 instead of an empty page.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


@dataclass
class ReaderSettings:
    neuraldeep_base_url: str
    neuraldeep_api_key: str
    judge_llm_url: str
    judge_llm_api_key: str
    judge_llm_model: str
    judge_timeout_s: float
    proxy_url: str
    playwright_url: str

    @classmethod
    def from_env(cls) -> "ReaderSettings":
        base = os.environ.get("NEURALDEEP_BASE_URL", "https://api.neuraldeep.ru").rstrip("/")
        key = os.environ.get("NEURALDEEP_API_KEY", "")
        return cls(
            neuraldeep_base_url=base,
            neuraldeep_api_key=key,
            judge_llm_url=os.environ.get("JUDGE_LLM_URL", f"{base}/v1"),
            judge_llm_api_key=os.environ.get("JUDGE_LLM_API_KEY", key),
            judge_llm_model=os.environ.get("JUDGE_LLM_MODEL", "qwen3.6-35b-a3b-noreason"),
            judge_timeout_s=float(os.environ.get("JUDGE_TIMEOUT_S", "4")),
            proxy_url=os.environ.get("PROXY_URL", ""),
            playwright_url=os.environ.get("PLAYWRIGHT_URL", "").rstrip("/"),
        )

    @property
    def judge_enabled(self) -> bool:
        return bool(self.judge_llm_api_key)


class ReaderUnavailable(Exception):
    """The step is not configured; the cascade treats it as a failed step."""


def neuraldeep_reader(settings: ReaderSettings):
    async def read(url: str, return_format: str) -> dict:
        if not settings.neuraldeep_api_key:
            raise ReaderUnavailable("no NEURALDEEP_API_KEY")
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            r = await client.post(
                f"{settings.neuraldeep_base_url}/v1/search/read",
                headers={"Authorization": f"Bearer {settings.neuraldeep_api_key}"},
                json={"url": url},
            )
        r.raise_for_status()
        d = r.json()
        return {"title": d.get("title") or "", "content": d.get("content") or "",
                "description": d.get("description") or ""}
    return read


def playwright_browser(settings: ReaderSettings):
    async def browse(url: str, use_proxy: bool) -> dict:
        if not settings.playwright_url:
            raise ReaderUnavailable("no PLAYWRIGHT_URL")
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            r = await client.post(f"{settings.playwright_url}/read", json={"url": url, "use_proxy": use_proxy})
        r.raise_for_status()
        d = r.json()
        return {"title": d.get("title") or "", "content": d.get("content") or ""}
    return browse
