"""NATS -> Temporal workflow dispatchers (ADR agents/016 §"Workflow dispatchers").

Dispatchers are the small adapters that turn canonical NATS events
(ADR 017 :class:`~projects.lakehouse.events.envelope.EventEnvelope`) into
Temporal ``start_workflow`` calls. ADR 016 §Proposal: "Workflow dispatchers are
small adapters (~30 lines each) that translate NATS events into ``start_workflow``
calls with deterministic IDs." The producer never references Temporal; the
consumer never references the producer.

Auto-discovery
--------------
Each dispatcher module dropped under this package exports a module-level
``DISPATCHERS`` list of :class:`Dispatcher`. New dispatcher = a new file; the
entrypoint (:mod:`projects.lakehouse.dispatchers.run`) discovers them through
:func:`all_dispatchers`, so there is **no shared registration list** to edit —
mirroring the ``orchestrator.workflows`` / ``orchestrator.schedules`` loaders and
keeping per-dispatcher units conflict-free.

A :class:`Dispatcher` carries:

* ``subject`` — the NATS subject to subscribe to (``events.{domain}.{type}``);
* ``durable`` — the durable (consumer-group) name so a restarted pod resumes
  from where it left off;
* ``handle`` — an ``async (envelope, temporal_client) -> None`` callable that
  reacts to one event (typically a ``start_workflow`` with a deterministic ID).

``handle`` takes the **decoded** :class:`EventEnvelope` (the run loop parses the
raw message once and applies the dispatcher's own ``event_type`` filter), plus a
``temporalio.client.Client``. Handlers stay independent of the workflow classes
they trigger: ADR 016 references workflows by **type-name string**, never by
import, so a dispatcher pulls in no workflow/activity deps.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import temporalio.client

    from projects.lakehouse.events.envelope import EventEnvelope

    # An event handler: react to one decoded envelope using a Temporal client.
    Handler = Callable[[EventEnvelope, "temporalio.client.Client"], Awaitable[None]]
else:  # at runtime the alias is only used for annotations, kept loose.
    Handler = Callable[..., Awaitable[None]]


@dataclasses.dataclass(frozen=True)
class Dispatcher:
    """One NATS-subject -> Temporal-workflow adapter.

    ``handle`` is typed loosely enough that importing this module (and running
    the hermetic loader tests) never forces a ``temporalio`` import at
    module-definition time. The submodules that build the actual handlers import
    ``temporalio`` themselves when they need its exception types.

    Attributes:
        subject: NATS subject to subscribe to (e.g. ``events.knowledge.gap``).
        durable: Durable consumer (consumer-group) name; a restarted pod resumes
            from the durable's position rather than replaying the whole stream.
        handle: ``async (envelope, temporal_client) -> None`` reacting to one
            event. The run loop only calls it for events this dispatcher cares
            about (see ``event_type``).
        event_type: Optional ADR-017 ``event_type`` filter. When set, the run
            loop dispatches only events whose ``event_type`` matches; when
            ``None`` the handler sees every event on ``subject``.
    """

    subject: str
    durable: str
    handle: Handler
    event_type: str | None = None

    def matches(self, envelope: Any) -> bool:
        """Whether ``envelope`` should be handed to :attr:`handle`.

        Applies the optional ``event_type`` filter (ADR 017 §"Per-consumer
        interpretation": consumers subscribe to a subject, then filter by
        ``event_type``). With no filter, every event on the subject matches.
        """
        if self.event_type is None:
            return True
        return getattr(envelope, "event_type", None) == self.event_type


def _iter_dispatcher_modules() -> list[ModuleType]:
    """Import and return every dispatcher module in this package.

    Skips private modules, test modules, and the ``run`` entrypoint — none of
    those export ``DISPATCHERS``.
    """
    modules: list[ModuleType] = []
    for info in pkgutil.iter_modules(__path__, prefix=__name__ + "."):
        leaf = info.name.rsplit(".", 1)[-1]
        if leaf.startswith("_") or leaf.endswith("_test") or leaf == "run":
            continue
        modules.append(importlib.import_module(info.name))
    return modules


def all_dispatchers() -> list[Dispatcher]:
    """Aggregate the ``DISPATCHERS`` list exported by every dispatcher module."""
    found: list[Dispatcher] = []
    for module in _iter_dispatcher_modules():
        found.extend(getattr(module, "DISPATCHERS", ()))
    return found


__all__ = [
    "Dispatcher",
    "Handler",
    "all_dispatchers",
]
