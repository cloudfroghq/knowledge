"""Bearer-token authentication middleware for the streamable-http transport.

Resolves the ``Authorization: Bearer <token>`` header to a principal
(:mod:`knowledge.principals`) and leaves it on the request for what runs
after: ``request.state.knowledge_principal`` holds its id and
``request.state.knowledge_scopes`` its scopes. A token that matches no
principal gets 401. Which scope a tool needs is decided by the tool itself
(``knowledge.server`` reads the scopes back through the MCP request context),
because the tool name travels inside the JSON-RPC body this middleware does
not read.

Routes that should NOT require auth (health checks and webhook endpoints
that authenticate via their own mechanism) are listed in
``_UNAUTHENTICATED_PREFIXES`` / ``_UNAUTHENTICATED_PATHS``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from knowledge import errors, principals

log = logging.getLogger(__name__)

# Exact paths that bypass auth.
_UNAUTHENTICATED_PATHS: frozenset[str] = frozenset({"/health"})

# Path prefixes that bypass the global Bearer middleware. The webhook endpoint
# enforces its own HMAC / Bearer auth inside the handler.
_UNAUTHENTICATED_PREFIXES: tuple[str, ...] = ("/webhook/",)


def _is_public(path: str) -> bool:
    if path in _UNAUTHENTICATED_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _UNAUTHENTICATED_PREFIXES)


def _unauthorized(message: str = "unauthorized") -> JSONResponse:
    return JSONResponse({"error": message}, status_code=401)


def _misconfigured(message: str) -> JSONResponse:
    return JSONResponse({"error": f"server misconfigured: {message}"}, status_code=500)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request that does not present the token of a known principal.

    The principals come from :meth:`principals.PrincipalTable.current`, so
    the process-wide configuration decides them (``MCP_API_KEY``,
    ``MCP_ADMIN_KEY``, ``bearers``). Tokens are compared in constant time.
    """

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path

        if _is_public(path):
            return await call_next(request)

        try:
            table = principals.PrincipalTable.current()
        except errors.KnowledgeConfigError as exc:
            log.error("Bearer configuration refused (%s). Refusing request to %s.", exc.code, path)
            return _misconfigured(exc.code)
        if not table:
            log.error(
                "No bearer is configured but HTTP transport is active (MCP_API_KEY, MCP_ADMIN_KEY "
                "or bearers). Refusing request to %s.",
                path,
            )
            return _misconfigured("MCP_API_KEY not set")

        provided = principals.bearer_token(request.headers.get("authorization"))
        if provided is None:
            return _unauthorized()

        principal = table.resolve(provided)
        if principal is None:
            return _unauthorized()

        request.state.knowledge_principal = principal.id
        request.state.knowledge_scopes = principal.scopes
        log.debug("principal=%s path=%s", principal.id, path)
        return await call_next(request)
