"""Residue of the public-tier notes chat domain (ADR 005).

The anonymous, internet-facing notes chat was retired in 2026-10 (#6913): its
router, admission, inference, retrieval and cache code are gone and the domain
is composed into no binary. What remains is what outlived the chat surface:

- ``retention`` and the ``chat_public`` schema it purges, driven by the
  ``chat-public-retention`` and ``chat-public-takedown`` CronWorkflows through
  ``app/jobs_main.py`` (the tables and their ``public_writer`` grants stay);
- ``models`` (the schema's SQLModel shape) and ``db`` (the public_writer
  engine) the retention core and its tests read;
- ``sse`` and its ``api`` re-export, the one surface other domains may import
  (``moving.chat`` renders its SSE frames with it).

It is still distinct from the private ``chat`` domain (Discord + /explore) and
shares no code with it. Grimoire chat (``grimoire_chat``) is the live public
chat surface; it was copied from this domain and shares its ``CHAT_PUBLIC_*``
budgets and the ``public_writer`` role.
"""
