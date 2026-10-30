"""Read-only client of the memory server over HTTP, standard library only.

``context_pack(include_memories=True)`` asks the memory server for the
observations of the vault's memory project that match the query. This module
is the :class:`~knowledge.ports.MemoryClient` that does it, plus the factory
that decides, per vault, whether a client may call at all.

The request is ``GET <url>/search?q=<query>&project=<project>&limit=<n>`` with
three headers on every call: ``Authorization: Bearer <MEMORY_HTTP_TOKEN>``,
``X-Memory-Client: cloudfrog-knowledge/<contract version>`` and
``Accept: application/json``. The whole call is bounded by a one-second
timeout, so a memory server that hangs costs a pack one second, never a stuck
tool.

Three rules hold whatever the memory server accepts:

* No bearer, no call. Without ``MEMORY_HTTP_TOKEN`` the factory hands out a
  client that raises ``E_MEMORY_TOKEN_MISSING`` and opens no connection.
* The bearer never leaves the machine in clear: a ``KNOWLEDGE_MEMORY_URL``
  that is not ``http`` on a loopback host disables the client
  (``E_MEMORY_URL_NOT_LOOPBACK``) and logs a warning when the vault opens.
  The default opener ignores ``http_proxy``/``HTTP_PROXY`` and every other
  proxy variable, so the socket connects to the loopback host the URL names
  and never to a proxy that would receive the bearer.
* Nothing is written: the client only reads.

Every failure is a :class:`~knowledge.errors.MemoryUnavailable` with a stable
code. Log lines name the code and the failure class (or the HTTP status),
never the bearer and never the request URL, which carries the query.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from knowledge import contract, errors
from knowledge.errors import MemoryUnavailable
from knowledge.ports import MemoryClient
from knowledge.settings import MemorySettings, VaultSpec, header_token_valid, is_loopback_http_url

log = logging.getLogger(__name__)

#: Seconds one memory call may take, connection included.
TIMEOUT_SECONDS = 1.0

#: ``X-Memory-Client`` value: who is calling, and under which tool contract.
CLIENT_ID = f"cloudfrog-knowledge/{contract.CONTRACT_VERSION}"

#: Keys of a memory hit this client keeps; ``rank`` only orders the fusion.
_HIT_KEYS = ("id", "type", "title", "content", "rank")

Opener = Callable[..., Any]

#: An opener without proxies: a ``ProxyHandler`` with an empty mapping
#: replaces the one ``urllib`` builds from the environment, so a proxy
#: variable never routes the loopback request (and its bearer) elsewhere.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class UnconfiguredMemoryClient:
    """A memory client that never calls: every search raises its ``code``.

    What the factory hands out when a vault cannot read memory (no bearer, no
    project, a non-loopback URL), so a caller can say why memory is missing
    instead of reporting an empty result.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def search(self, query: str, *, project: str, limit: int = 5) -> list[dict[str, Any]]:
        del query, project, limit
        raise MemoryUnavailable(self.code, self.message)


class HttpMemoryClient:
    """``GET /search`` on the memory server, with the bearer on every request."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = TIMEOUT_SECONDS,
        opener: Opener = _NO_PROXY_OPENER.open,
    ) -> None:
        self._base = url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._open = opener

    def __repr__(self) -> str:
        return f"HttpMemoryClient(url={self._base!r})"

    def search(self, query: str, *, project: str, limit: int = 5) -> list[dict[str, Any]]:
        """Hits of ``project`` for ``query``, best first; ``[]`` when there are none.

        Raises :class:`~knowledge.errors.MemoryUnavailable`:
        ``E_MEMORY_TOKEN_MISSING`` or ``E_MEMORY_PROJECT_MISSING`` before any
        connection, ``E_MEMORY_UNAVAILABLE`` for a refused connection, a
        timeout, a status other than 200 or a body that is not a JSON list.
        """
        if not self._token:
            raise MemoryUnavailable(errors.E_MEMORY_TOKEN_MISSING, "MEMORY_HTTP_TOKEN is not set")
        if not header_token_valid(self._token):
            raise MemoryUnavailable(
                errors.E_MEMORY_TOKEN_MISSING, "MEMORY_HTTP_TOKEN holds whitespace or characters a bearer cannot carry"
            )
        if not project:
            raise MemoryUnavailable(
                errors.E_MEMORY_PROJECT_MISSING, "the vault declares no memory project (memory.project)"
            )
        params = urllib.parse.urlencode({"q": query, "project": project, "limit": str(limit)})
        request = urllib.request.Request(
            f"{self._base}/search?{params}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-Memory-Client": CLIENT_ID,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._open(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise self._unavailable(f"HTTP {exc.code}") from None
        except OSError as exc:
            # URLError, a refused connection and a timeout are all OSError;
            # only the class of the cause is logged, never its text, which
            # may carry the URL.
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            cause = type(reason).__name__ if isinstance(reason, BaseException) else "URLError"
            raise self._unavailable(cause) from None
        if status != 200:
            raise self._unavailable(f"HTTP {status}")
        try:
            payload = json.loads(body)
        except ValueError:
            raise self._unavailable("response is not JSON") from None
        # The memory server answers ``null``, not ``[]``, when nothing matches.
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise self._unavailable("response is not a JSON list")
        return [{key: hit[key] for key in _HIT_KEYS if key in hit} for hit in payload if isinstance(hit, dict)]

    @staticmethod
    def _unavailable(reason: str) -> MemoryUnavailable:
        log.warning("memory search failed: %s (%s)", errors.E_MEMORY_UNAVAILABLE, reason)
        return MemoryUnavailable(errors.E_MEMORY_UNAVAILABLE, f"the memory server could not be read ({reason})")


def memory_client_for(spec: VaultSpec, *, settings: MemorySettings) -> MemoryClient:
    """The memory client of vault ``spec``: a real one only when it may call.

    The checks run in this order, each with its own code: a bearer
    (``E_MEMORY_TOKEN_MISSING``), a loopback ``http`` URL
    (``E_MEMORY_URL_NOT_LOOPBACK``, also logged as a warning) and a memory
    project (``E_MEMORY_PROJECT_MISSING``).
    """
    if not settings.token:
        return UnconfiguredMemoryClient(errors.E_MEMORY_TOKEN_MISSING, "MEMORY_HTTP_TOKEN is not set")
    if not is_loopback_http_url(settings.url):
        log.warning(
            "%s: %s: KNOWLEDGE_MEMORY_URL is not http on a loopback host; memory reads are disabled "
            "so the bearer never leaves this machine",
            errors.E_MEMORY_URL_NOT_LOOPBACK,
            spec.slug,
        )
        return UnconfiguredMemoryClient(
            errors.E_MEMORY_URL_NOT_LOOPBACK, "KNOWLEDGE_MEMORY_URL must be http on 127.0.0.1, localhost or ::1"
        )
    if not spec.memory_project:
        return UnconfiguredMemoryClient(
            errors.E_MEMORY_PROJECT_MISSING, f"{spec.slug}: the vault declares no memory project (memory.project)"
        )
    return HttpMemoryClient(settings.url, settings.token)


def factory(settings: MemorySettings) -> Callable[[VaultSpec], MemoryClient]:
    """``memory_client_for`` bound to ``settings``, in the shape a registry takes."""

    def build(spec: VaultSpec) -> MemoryClient:
        return memory_client_for(spec, settings=settings)

    return build
