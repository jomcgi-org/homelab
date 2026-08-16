"""Private-tier module registry: every domain the confined monolith composes.

Registration order is load-bearing for route matching precedence and mirrors
the historical hand-authored order in app/main.py exactly; append new domains
at the end of their group (routed domains before MCP-only domains) unless a
route overlap forces otherwise.

This file must NEVER be imported by the public entrypoint: it imports the
private domains (chat, agent, demos, ...) that are pruned from the public
binary's file set. The public registry lives in app/modules_public.py.
"""

from __future__ import annotations

import agent.module
import agent_sessions.module
import artifact.module
import campsites.module
import chat.module
import cluster.module
import demos.module
import dr_jobs.module
import ember_public.module
import faas.module
import swarm.module
import grimoire.module
import hikes.module
import home.module
import knowledge.module
import moving.module
import sandbox.module
import scheduler.module
import semgrep_scan.module
import ships.module
import stars.module
import trips.module
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
    agent_sessions.module.MODULE,
    worldcup.module.MODULE,
    artifact.module.MODULE,
    faas.module.MODULE,
    swarm.module.MODULE,
    demos.module.MODULE,
    ember_public.module.MODULE,
    moving.module.MODULE,
    # MCP-only domains (no HTTP routes of their own). Placed here, before
    # semgrep_scan, so MCP tool registration order matches the historical
    # app/main.py import order (knowledge, agent, cluster, semgrep_scan,
    # sandbox); route order is unaffected because these mount no routes.
    agent.module.MODULE,
    cluster.module.MODULE,
    # GitHub PR webhook -> fc-invoke scan -> Semgrep App relay. Registers
    # POST /webhooks/github/semgrep; HMAC-verified, no cf-access on that path.
    semgrep_scan.module.MODULE,
    sandbox.module.MODULE,
)

# Domain names composable as standalone binaries via app/main_domain.py.
# Kept in sync with MONOLITH_DOMAINS in projects/monolith/domain_images.bzl
# (the Bazel image fan-out); app/main_domain_test.py smoke-composes each.
DOMAIN_NAMES: tuple[str, ...] = tuple(m.name for m in ALL_MODULES)
