"""Residue of the public-tier notes chat domain (ADR 005).

The anonymous, internet-facing notes chat was retired in 2026-10 (#6913): its
router, admission, inference, retrieval and cache code are gone and the domain
is composed into no binary. What remains is what outlived the chat surface:

- ``models`` (the schema's SQLModel shape) and ``db`` (the public_writer
  engine), retained alongside the historical schema and grants;
- ``sse`` and its ``api`` re-export, the one surface other domains may import
  (``moving.chat`` renders its SSE frames with it).

It is still distinct from the private ``chat`` domain (Discord + /explore) and
shares no code with it. Grimoire chat (``grimoire_chat``) is the live public
chat surface; it was copied from this domain and shares its ``CHAT_PUBLIC_*``
budgets and the ``public_writer`` role.

The compatibility-named purge jobs now target ``grimoire_chat.retention``.
No mounted repository writer uses ``chat_public``; accepting removal of its
purge path still requires checking deployed writers (#7038).
"""
