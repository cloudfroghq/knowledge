"""Who an HTTP bearer belongs to and what it may do.

A principal is an id and a set of scopes: ``read`` uses the server, ``admin``
also changes the index or the vault (``reindex``, ``sync_confluence``,
``POST /webhook/reindex``). Which tokens map to which principal follows the
settings, in this order:

* ``bearers`` in ``vaults.yaml``: one principal per entry, and nothing else.
  ``MCP_API_KEY`` and ``MCP_ADMIN_KEY`` are not added on top, so an entry may
  reuse either variable without colliding with it;
* ``MCP_ADMIN_KEY`` set: ``MCP_ADMIN_KEY`` is ``admin`` (``read`` and
  ``admin``) and ``MCP_API_KEY``, if set, is ``service-read`` (``read``);
* neither: ``MCP_API_KEY`` is ``service`` with ``read`` and ``admin``, the
  single bearer every client presented before scopes existed.

Resolving a token compares it with every entry, always all of them, in
constant time per entry, so the time it takes says nothing about which entry
(if any) matched. Only ids and scopes ever leave this module; a token never
reaches a log line, an error or a repr.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from knowledge import config, errors, settings

READ = settings.SCOPE_READ
ADMIN = settings.SCOPE_ADMIN

#: Principal of ``MCP_API_KEY`` when no scope is configured: it may do everything.
SERVICE_ID = "service"
#: Principal of ``MCP_API_KEY`` once ``MCP_ADMIN_KEY`` is set.
SERVICE_READ_ID = "service-read"
#: Principal of ``MCP_ADMIN_KEY``.
ADMIN_ID = "admin"

_BEARER_PREFIX = "Bearer "


@dataclass(frozen=True)
class Principal:
    """An authenticated caller: an id safe to log and the scopes it holds."""

    id: str
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        """Whether this principal holds ``scope``."""
        return scope in self.scopes


@dataclass(frozen=True)
class Declared:
    """A principal as the settings declare it: the variable its token comes from and whether it is set."""

    principal: Principal
    token_env: str
    token_set: bool


def _declared(
    api_key: str, admin_key: str, bearers: Sequence[settings.BearerSpec]
) -> list[tuple[Declared, str]]:
    """Every principal the settings declare, with its token (empty when unset), by the rules above."""
    if bearers:
        return [
            (Declared(Principal(b.id, frozenset(b.scopes)), b.token_env, bool(b.token)), b.token) for b in bearers
        ]
    if admin_key:
        return [
            (Declared(Principal(ADMIN_ID, frozenset({READ, ADMIN})), "MCP_ADMIN_KEY", True), admin_key),
            (Declared(Principal(SERVICE_READ_ID, frozenset({READ})), "MCP_API_KEY", bool(api_key)), api_key),
        ]
    return [(Declared(Principal(SERVICE_ID, frozenset({READ, ADMIN})), "MCP_API_KEY", bool(api_key)), api_key)]


def declared(s: settings.Settings) -> tuple[Declared, ...]:
    """The principals ``s`` declares, tokens left out: what ``doctor`` reports."""
    return tuple(row for row, _ in _declared(s.http.api_key, s.http.admin_key, s.bearers))


def bearer_token(header: str | None) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header; ``None`` when there is none."""
    if not header or not header.startswith(_BEARER_PREFIX):
        return None
    token = header[len(_BEARER_PREFIX) :].strip()
    return token or None


class PrincipalTable:
    """Every token this process accepts, each with its principal."""

    def __init__(self, entries: Sequence[tuple[str, Principal]]) -> None:
        # An empty token never authenticates anyone, so it is not an entry.
        self._entries: tuple[tuple[bytes, Principal], ...] = tuple(
            (token.encode("utf-8"), principal) for token, principal in entries if token
        )

    def __repr__(self) -> str:
        return f"PrincipalTable({[p.id for p in self.principals()]!r})"

    def __len__(self) -> int:
        return len(self._entries)

    def principals(self) -> tuple[Principal, ...]:
        """The principals, in declaration order."""
        return tuple(principal for _, principal in self._entries)

    @classmethod
    def from_values(
        cls, *, api_key: str, admin_key: str, bearers: Iterable[settings.BearerSpec]
    ) -> PrincipalTable:
        """The table the settings describe (see the module docstring for the rules).

        Raises :class:`~knowledge.errors.KnowledgeConfigError` with
        ``E_MANIFEST_INVALID`` when one token would stand for two principals,
        which :meth:`settings.Settings.validate` already refuses at startup:
        a table that cannot tell read from admin is never built.
        """
        rows = _declared(api_key, admin_key, tuple(bearers))
        tokens = [token for _, token in rows if token]
        if len(set(tokens)) != len(tokens):
            raise errors.KnowledgeConfigError(
                errors.E_MANIFEST_INVALID,
                "one token stands for two principals (a duplicate bearer value, or MCP_ADMIN_KEY "
                "equal to MCP_API_KEY); every principal needs its own token",
            )
        return cls([(token, row.principal) for row, token in rows])

    @classmethod
    def from_settings(cls, s: settings.Settings) -> PrincipalTable:
        """The table of ``s``."""
        return cls.from_values(api_key=s.http.api_key, admin_key=s.http.admin_key, bearers=s.bearers)

    @classmethod
    def current(cls) -> PrincipalTable:
        """The table of the process-wide configuration.

        The two keys are read through :mod:`knowledge.config`, so a value
        assigned there is the one honoured, as for every other setting of
        the HTTP surface.
        """
        return cls.from_values(
            api_key=str(config.MCP_API_KEY or ""),
            admin_key=str(config.MCP_ADMIN_KEY or ""),
            bearers=settings.current().bearers,
        )

    def resolve(self, token: str) -> Principal | None:
        """The principal ``token`` belongs to, or ``None``.

        Every entry is compared, whatever matched first.
        """
        provided = token.encode("utf-8")
        found: Principal | None = None
        for expected, principal in self._entries:
            if secrets.compare_digest(provided, expected) and found is None:
                found = principal
        return found
