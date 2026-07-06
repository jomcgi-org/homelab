"""Public-tier grimoire chat domain (ADR security/005 posture).

The anonymous, internet-facing chat surface over the Grimoire (D&D sourcebook)
corpus, composed into the public binary only. Like ``chat_public`` it is distinct
from the private ``chat`` domain and shares no code with it: the public binary
must never import anything under ``chat/`` (enforced by
``app/main_public_imports_test.py`` and pruned by ``_PUBLIC_PRUNE_EXCLUDE`` in
``projects/monolith/BUILD``). The router mounts under an internal prefix and is
never added to the public HTTPRoute; the SSR app is the only public origin.

This is a public-tier-only domain, so it exposes ``register_public`` (and an
alias ``register``); there is no private surface.
"""

from __future__ import annotations

from fastapi import FastAPI

__all__ = ["register", "register_public"]


def register_public(app: FastAPI) -> None:
    """Register the internal grimoire-chat routes on the public app."""
    from grimoire_chat.router import router

    app.include_router(router)


# Public-tier-only: there is no separate private surface, so register aliases
# register_public for any composition path that calls register().
register = register_public
