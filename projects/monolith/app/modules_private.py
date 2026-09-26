"""Private-tier module registry: every domain the confined monolith composes.

Registration order is load-bearing for route matching precedence and mirrors
the historical hand-authored order in app/main.py exactly; append new domains
at the end of their group (routed domains before MCP-only domains) unless a
route overlap forces otherwise.

This file must NEVER be imported by the public entrypoint: it imports the
private domains (chat, agent, ...) that are pruned from the public
binary's file set. The public registry lives in app/modules_public.py.
"""

from __future__ import annotations

import agent.module
import factory.module
import artifact.module
import campsites.module
import chat.module
import cluster.module
import dr_jobs.module
import ember_public.module
import faas.module
import grimoire.module
import hikes.module
import home.module
import knowledge.module
import moving.module
import observability.module
import sandbox.module
import scheduler.module
import shotter.module
import ships.module
import stars.module
import trips.module
import updates.module
import worldcup.module
from framework import Module

ALL_MODULES: tuple[Module, ...] = (
    # Routed domains, in the historical app/main.py registration order.
    home.module.MODULE,
    chat.module.MODULE,
    knowledge.module.MODULE,
    scheduler.module.MODULE,
    ships.module.MODULE,
    grimoire.module.MODULE,
    hikes.module.MODULE,
    stars.module.MODULE,
    trips.module.MODULE,
    dr_jobs.module.MODULE,
    campsites.module.MODULE,
    factory.module.MODULE,
    observability.module.MODULE,
    worldcup.module.MODULE,
    artifact.module.MODULE,
    faas.module.MODULE,
    ember_public.module.MODULE,
    moving.module.MODULE,
    updates.module.MODULE,
    # MCP-only domains (no HTTP routes of their own). Placed here so MCP tool
    # registration order matches the historical app/main.py import order
    # (knowledge, agent, cluster, sandbox); route order is unaffected because
    # these mount no routes.
    agent.module.MODULE,
    cluster.module.MODULE,
    sandbox.module.MODULE,
    shotter.module.MODULE,
)

# Domain names composable as standalone binaries via app/main_domain.py.
# Kept in sync with MONOLITH_DOMAINS in projects/monolith/domain_images.bzl
# (the Bazel image fan-out); app/main_domain_test.py smoke-composes each.
DOMAIN_NAMES: tuple[str, ...] = tuple(m.name for m in ALL_MODULES)
