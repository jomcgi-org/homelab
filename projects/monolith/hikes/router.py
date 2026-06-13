"""Hikes HTTP API. SSR-only: never added to httproute-public.yaml.

Reached only from SvelteKit SSR (``http://localhost:8000`` in the same pod);
the /app/hikes page is the public surface and the CDN fans out to viewers.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger("hikes")

router = APIRouter(prefix="/api/hikes", tags=["hikes"])
