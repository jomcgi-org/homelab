from __future__ import annotations

from importlib import import_module


def lazy_exports(module_name: str, exports: dict[str, str]):
    def __getattr__(name: str):
        module = exports.get(name)
        if module is None:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
        return getattr(import_module(module), name)

    return __getattr__
