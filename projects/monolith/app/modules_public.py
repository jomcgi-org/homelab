"""Public-tier module registry: only domains with a public, read-only surface.

Imports ONLY public-safe domains: the pruned public binary excludes the
private domain files entirely, so importing a private domain here would crash
the public image at startup and trip app/main_public_imports_test.py.

Registration order mirrors the historical hand-authored order in
app/main_public.py exactly (route matching precedence).
"""

from __future__ import annotations

import artifact.module
import campsites.module
import chat_public.module
import dr_jobs.module
import grimoire.module
import grimoire_chat.module
import hikes.module
import home.module
import knowledge.module
import ships.module
import stars.module
import trips.module
import worldcup.module
from framework import Module

PUBLIC_MODULES: tuple[Module, ...] = (
    ships.module.MODULE,
    stars.module.MODULE,
    trips.module.MODULE,
    hikes.module.MODULE,
    dr_jobs.module.MODULE,
    campsites.module.MODULE,
    worldcup.module.MODULE,
    knowledge.module.MODULE,
    home.module.MODULE,
    chat_public.module.MODULE,
    artifact.module.MODULE,
    grimoire.module.MODULE,
    grimoire_chat.module.MODULE,
)
