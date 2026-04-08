"""Vertex AI Gemini provider with ADC authentication, proxy support, and multi-location failover.

This module provides :class:`VertexGeminiChatModel`, which wraps
:class:`~deerflow.models.patched_openai.PatchedChatOpenAI` to add:

1. **Google Cloud ADC authentication** — uses Application Default Credentials or a
   service-account JSON key file to obtain a short-lived OAuth2 bearer token, which
   is used as the OpenAI-compatible API key.  The token is refreshed automatically
   ~60 seconds before expiry, and token-refresh HTTP calls also go through the
   configured proxy.

2. **Proxy support** — a ``proxy`` URL (e.g.
   ``http://user:pass@host:port``) is passed to both the Google auth session and the
   OpenAI-compatible HTTP client so all traffic routes through it.

3. **Multi-location failover** — accepts a ``vertex_locations`` list.  On transient
   errors (HTTP 429 / 500 / 502 / 503 / 504, connection errors, timeouts) the provider
   advances to the next location and retries transparently.  The last successful
   location is remembered so subsequent calls prefer it.

Endpoint URL format
-------------------
* **global**: ``https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/endpoints/openapi``
* **regional**: ``https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/endpoints/openapi``

Usage in ``config.yaml``::

    - name: gemini-vertex
      display_name: Gemini 2.5 Pro (Vertex AI)
      use: deerflow.models.vertex_gemini_provider:VertexGeminiChatModel
      model: google/gemini-2.5-pro
      vertex_project: gemini-0512
      vertex_locations:
        - global
        - us-central1
      proxy: http://user:pass@43.153.88.47:9529   # optional
      # vertex_credentials_path: /path/to/service-account.json  # omit to use ADC
      request_timeout: 600.0
      max_tokens: 65535
      temperature: 0.01
      supports_thinking: true
      supports_vision: true
      when_thinking_enabled:
        extra_body:
          thinking:
            type: enabled
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from pydantic import Field, PrivateAttr, SecretStr, model_validator

from .patched_openai import PatchedChatOpenAI

logger = logging.getLogger(__name__)

# HTTP status codes that warrant switching to a different Vertex AI location.
_SWITCHABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _build_base_url(project: str, location: str) -> str:
    """Return the Vertex AI OpenAI-compatible base URL for a given location.

    The ``global`` pseudo-location uses the non-regional hostname.
    All other location strings are treated as regional identifiers.
    """
    if location == "global":
        return f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/endpoints/openapi"
    return (
        f"https://{location}-aiplatform.googleapis.com/v1"
        f"/projects/{project}/locations/{location}/endpoints/openapi"
    )


def _is_location_switchable(exc: Exception) -> bool:
    """Return True if *exc* is a transient error that warrants a location switch."""
    try:
        import openai as _openai

        if isinstance(exc, _openai.APIStatusError):
            return exc.status_code in _SWITCHABLE_STATUS_CODES
        if isinstance(exc, (_openai.APIConnectionError, _openai.APITimeoutError)):
            return True
    except ImportError:
        pass
    return False


class VertexGeminiChatModel(PatchedChatOpenAI):
    """Gemini on Vertex AI via the OpenAI-compatible endpoint.

    Extends :class:`~deerflow.models.patched_openai.PatchedChatOpenAI` so that
    ``thought_signature`` values are preserved across multi-turn tool-call
    conversations when Gemini thinking is enabled.

    Parameters
    ----------
    vertex_project:
        Google Cloud project ID (e.g. ``"gemini-0512"``).
    vertex_locations:
        Ordered list of Vertex AI locations to try.  Use ``"global"`` for the
        global endpoint, or a region code like ``"us-central1"``.  On a
        transient error the provider cycles to the next entry and retries.
    vertex_credentials_path:
        Path to a service-account JSON key file.  Omit to use Application
        Default Credentials (ADC) configured via
        ``gcloud auth application-default login`` or the
        ``GOOGLE_APPLICATION_CREDENTIALS`` environment variable.
    proxy:
        Optional HTTP/HTTPS proxy URL (e.g.
        ``"http://user:pass@host:port"``).  Applied to both the Google
        token-refresh session and the OpenAI-compatible API client.
    """

    vertex_project: str = Field(..., description="Google Cloud project ID")
    vertex_locations: list[str] = Field(
        ...,
        min_length=1,
        description="Vertex AI locations tried in order (e.g. ['global', 'us-central1'])",
    )
    vertex_credentials_path: str | None = Field(
        default=None,
        description="Path to a service-account JSON key file; omit to use ADC.",
    )
    proxy: str | None = Field(
        default=None,
        description="HTTP(S) proxy URL for all Vertex AI and token-refresh traffic.",
    )

    # --- private runtime state (not serialised by Pydantic) ---
    _sync_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _async_lock: asyncio.Lock | None = PrivateAttr(default=None)
    _cached_token: str | None = PrivateAttr(default=None)
    _token_expiry: float = PrivateAttr(default=0.0)
    _current_location_idx: int = PrivateAttr(default=0)

    # ------------------------------------------------------------------
    # Pydantic lifecycle
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _inject_vertex_defaults(cls, data: Any) -> Any:
        """Inject placeholder ``openai_api_key`` / ``openai_api_base`` so that
        the parent ChatOpenAI validators pass during construction.  Real values
        are set in :meth:`model_post_init`.
        """
        if not isinstance(data, dict):
            return data
        import os

        if not data.get("openai_api_key") and not os.environ.get("OPENAI_API_KEY"):
            data.setdefault("openai_api_key", "vertex-ai-adc-placeholder")
        if not data.get("openai_api_base") and not data.get("base_url"):
            project = data.get("vertex_project", "")
            locations = data.get("vertex_locations") or []
            if project and locations:
                data["openai_api_base"] = _build_base_url(project, locations[0])
        return data

    def model_post_init(self, __context: Any) -> None:
        token = self._refresh_token_sync()
        location = self.vertex_locations[0]
        self.openai_api_key = SecretStr(token)
        self.openai_api_base = _build_base_url(self.vertex_project, location)
        super().model_post_init(__context)
        self._async_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _get_auth_session(self):
        """Return a ``requests.Session`` configured with the proxy (if any)."""
        import requests as _requests

        session = _requests.Session()
        if self.proxy:
            session.proxies.update({"http": self.proxy, "https": self.proxy})
        return session

    def _refresh_token_sync(self) -> str:
        """Return a valid Google Cloud OAuth2 bearer token (thread-safe, cached)."""
        now = time.time()
        if self._cached_token and now < self._token_expiry - 60:
            return self._cached_token

        import google.auth
        import google.auth.transport.requests

        if self.vertex_credentials_path:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                self.vertex_credentials_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        else:
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )

        request = google.auth.transport.requests.Request(session=self._get_auth_session())
        creds.refresh(request)
        self._cached_token = creds.token
        self._token_expiry = (
            creds.expiry.timestamp() if creds.expiry else time.time() + 3600
        )
        logger.debug(
            "Refreshed Vertex AI auth token (expires in ~%ds)",
            int(self._token_expiry - now),
        )
        return self._cached_token

    async def _refresh_token_async(self) -> str:
        """Refresh token asynchronously by delegating to the sync version via a thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._refresh_token_sync)

    # ------------------------------------------------------------------
    # Per-location client factories
    # ------------------------------------------------------------------

    def _make_sync_client(self, location: str, token: str):
        """Build a sync OpenAI chat-completions client for *location*."""
        import httpx
        import openai as _openai

        timeout = self.request_timeout if self.request_timeout is not None else 600.0
        http_client = httpx.Client(proxy=self.proxy) if self.proxy else None
        return _openai.OpenAI(
            api_key=token,
            base_url=_build_base_url(self.vertex_project, location),
            timeout=timeout,
            max_retries=0,  # location-level retry handled by us
            http_client=http_client,
        ).chat.completions

    def _make_async_client(self, location: str, token: str):
        """Build an async OpenAI chat-completions client for *location*."""
        import httpx
        import openai as _openai

        timeout = self.request_timeout if self.request_timeout is not None else 600.0
        http_client = httpx.AsyncClient(proxy=self.proxy) if self.proxy else None
        return _openai.AsyncOpenAI(
            api_key=token,
            base_url=_build_base_url(self.vertex_project, location),
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        ).chat.completions

    # ------------------------------------------------------------------
    # Sync generate with location failover
    # ------------------------------------------------------------------

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        with self._sync_lock:
            return self._generate_with_failover(messages, stop, run_manager, **kwargs)

    def _generate_with_failover(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None,
        run_manager: CallbackManagerForLLMRun | None,
        **kwargs: Any,
    ) -> ChatResult:
        locations = self.vertex_locations
        last_error: Exception | None = None
        original_client = self.client

        for attempt in range(len(locations)):
            loc_idx = (self._current_location_idx + attempt) % len(locations)
            location = locations[loc_idx]
            try:
                token = self._refresh_token_sync()
                self.client = self._make_sync_client(location, token)
                result = super()._generate(messages, stop, run_manager, **kwargs)
                self._current_location_idx = loc_idx
                return result
            except Exception as exc:
                is_last = attempt >= len(locations) - 1
                if _is_location_switchable(exc) and not is_last:
                    next_loc = locations[(loc_idx + 1) % len(locations)]
                    logger.warning(
                        "Vertex AI location %s failed (%s: %s); switching to %s",
                        location,
                        type(exc).__name__,
                        exc,
                        next_loc,
                    )
                    last_error = exc
                else:
                    self.client = original_client
                    raise
            else:
                self.client = original_client

        assert last_error is not None
        self.client = original_client
        raise last_error

    # ------------------------------------------------------------------
    # Async generate with location failover
    # ------------------------------------------------------------------

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        assert self._async_lock is not None
        async with self._async_lock:
            return await self._agenerate_with_failover(messages, stop, run_manager, **kwargs)

    async def _agenerate_with_failover(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None,
        run_manager: AsyncCallbackManagerForLLMRun | None,
        **kwargs: Any,
    ) -> ChatResult:
        locations = self.vertex_locations
        last_error: Exception | None = None
        original_async_client = self.async_client

        for attempt in range(len(locations)):
            loc_idx = (self._current_location_idx + attempt) % len(locations)
            location = locations[loc_idx]
            try:
                token = await self._refresh_token_async()
                self.async_client = self._make_async_client(location, token)
                result = await super()._agenerate(messages, stop, run_manager, **kwargs)
                self._current_location_idx = loc_idx
                return result
            except Exception as exc:
                is_last = attempt >= len(locations) - 1
                if _is_location_switchable(exc) and not is_last:
                    next_loc = locations[(loc_idx + 1) % len(locations)]
                    logger.warning(
                        "Vertex AI location %s failed (%s: %s); switching to %s",
                        location,
                        type(exc).__name__,
                        exc,
                        next_loc,
                    )
                    last_error = exc
                else:
                    self.async_client = original_async_client
                    raise
            else:
                self.async_client = original_async_client

        assert last_error is not None
        self.async_client = original_async_client
        raise last_error
