"""Ships HTTP API. SSR-only: never added to httproute-public.yaml."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("ships")
router = APIRouter(prefix="/api/ships", tags=["ships"])
