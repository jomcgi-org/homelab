"""FastMonolith module export for public observability routes."""

import observability as _domain

from framework import Module as _Module


MODULE = _Module(
    name="observability",
    register_public=_domain.register_public,
)
