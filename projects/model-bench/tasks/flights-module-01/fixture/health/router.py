"""Health router: a minimal APIRouter mounted under /api/health."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
def health() -> dict:
    return {"status": "ok"}
