# Manual and external work

The private Factory records work performed by a named human or external owner.
Recording a task sends no message and starts no executor. Source URLs are
references: the recording API never fetches or synchronizes them.

## Private operations

`factory_tracking_create`, `factory_tracking_read`, `factory_tracking_list`,
`factory_tracking_update` and `factory_tracking_complete` require the existing
standing authenticated human operator gate. The server derives the event
author and request-key scope from that principal's subject. Owner strings,
source text and links are descriptive, with no permission meaning. Browser
board/detail reads use the private tier's verified Access email; they do not
accept a caller-declared operator or expose recording controls.

Create takes a stable `request_key`, title, HTTPS `source_ref`, `tracking_kind`
(`manual` or `external`) and named `owner`. Body, blocker and next action are
optional bounded text. It returns the durable ID and `tracking_version=1`.
The source, owner, kind, execution mode and authority are immutable. Changing
source or owner requires recording a separate task and closing the old one;
there is no hard-delete operation.

Source identity is shared across operator subjects and tracking kinds. Its
canonical form lowercases and IDNA-normalizes the HTTPS host, removes the
default port and fragment, and uses `/` for an empty path. Path and query
remain case-sensitive and significant. A second request for that canonical
source is refused; it cannot claim or modify an executable GitHub work item
whose reference happens to be the same URL. Request keys are scoped to the
authenticated operator subject. Exact retries return the original recorded
snapshot, even after later updates. Conflicting reuse fails. To reconcile an
uncertain save, retry the identical request or read its returned durable ID.

Update and complete require `expected_version` and a new `request_key`.
They commit the state, attributable event and replay result in one transaction.
A database unique key serializes concurrent request-key claims. A partial
unique index serializes source identity. Conditional updates compare the
expected version in the database, so two different requests for one version
cannot both succeed. No process lock is the storage guarantee.

## Recorded states

Create starts `open`. Any of `open`, `ready`, `deferred`, `needs_human` and
`active` can change to any other of those states, `done`, or `closed`.
Same-state edits are allowed. `done` and `closed` may be reopened only to
`open`. Reopen preserves prior completion evidence, completion timestamp and
event history. A later completion adds another event and updates the latest
completion evidence and timestamp. Closing records `not_planned`.

Entering `done` requires nonblank completion evidence. The complete operation
also requires `state=done`. This is recorded external completion. Factory
accepted delivery remains a separate receipt-based result. `ready` and
`active` are descriptive states here: they grant no admission, worker capacity
or budget. Generic factory transition and relationship APIs refuse these rows.

List accepts one state and/or tracking kind, page sizes 1 through 100 and a
returned cursor bound to those filters. Pagination is descending durable ID,
without a snapshot guarantee. Read returns the latest row and last 20 events.
The private board displays at most 100 tracking records in its normal read and
poll cycle. Missing source/owner data renders as unknown. Text is escaped and
source links accept HTTPS without embedded credentials.

## Execution and publication boundaries

The additive migration defaults historical work to `execution_mode=factory`.
Tracking rows require local authority, `mcp` provenance and null GitHub
execution/pointer keys. PostgreSQL triggers forbid execution-mode conversion,
tracking identity changes, hard deletion and receipt links to tracking rows.
No production migration is part of repository implementation.

The local intake query selects factory mode explicitly. Direct receipt
submission rejects tracking IDs before minting or audit effects. Admission,
recovery, refinement and start reservation recheck linked mode, including
mislinked receipts. Pointer synchronization and the public work-item producer
select factory mode. Public relationships require two selected endpoints;
work-item events stay private. Receipt-derived public cards and outcome counts
also exclude linked tracking identities. `public_reader` receives no grant on
the request ledger. The public binary composes its existing snapshot reader;
no tracking MCP tools are registered there.

These changes extend the durable work-item model and the native private UI.
They add no scheduler, personal-task migration, webhook activation or external
ownership synchronization. #7154 retains its own live UI acceptance.
