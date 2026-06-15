# FastMonolith Framework — Implementation Design

**Date:** 2026-06-15
**ADR:** [services/010-fastmonolith-modular-framework](../decisions/services/010-fastmonolith-modular-framework.md)
**Builds on:** [security/004-public-read-only-service-isolation](../decisions/security/004-public-read-only-service-isolation.md)
**Branch:** `claude/monolith-framework-design-3wnz1w`

---

## Scope

Extract a small in-repo framework (`projects/monolith/framework/`) that composes deployable FastAPI binaries from privilege-typed, data-isolated domain modules, and use it to ship ADR 004's public/private split as the first consumer. Three enforcement layers, led by data isolation:

1. **Data** — per-domain Postgres schema, per-tier grants (the load-bearing boundary; step one).
2. **Build** — per-domain Bazel `py_library` with split `:_api` / internals visibility; disjoint binary `deps`.
3. **Runtime** — one `build_app(profile, modules)` owning all wiring; composes scheduler and MCP per binary.

Out of scope: per-domain production charts, separate databases per domain, any change to ADR 004's replica / NetworkPolicy / rollup decisions (consumed as-is).

## The framework surface

`framework/` is intentionally tiny. Three concepts.

### `Profile` — what a binary is allowed to do

```python
class Tier(enum.Enum):
    PUBLIC = "public"
    PRIVATE = "private"

@dataclasses.dataclass(frozen=True)
class Profile:
    tier: Tier
    db_endpoint_env: str          # "DATABASE_URL" (private) / "DATABASE_RO_URL" (public)
    db_role: str                  # "monolith_app" / "public_reader"
    db_read_only: bool            # public: True -> engine sets default_transaction_read_only
    allowed_secrets: frozenset[str]
    clickhouse_enabled: bool
    mcp_enabled: bool

PRIVATE_PROFILE = Profile(Tier.PRIVATE, "DATABASE_URL", "monolith_app", False, ALL_SECRETS, True, True)
PUBLIC_PROFILE  = Profile(Tier.PUBLIC,  "DATABASE_RO_URL", "public_reader", True, frozenset(), False, False)
```

### `Module` — what a domain provides

```python
@dataclasses.dataclass(frozen=True)
class Module:
    name: str
    tier: Tier
    schema: str                                   # owning Postgres schema
    register: Callable[[FastAPI], None]
    startup_jobs: Callable[[Session], None] | None = None
    register_mcp: Callable[[Any], None] | None = None    # given the shared MCP instance
    requires_secrets: frozenset[str] = frozenset()
    requires_clickhouse: bool = False
```

Each domain exports `MODULE = Module(...)`, reusing its existing `register` / `on_startup_jobs` as the callables, so the wrapper is thin. Cross-domain logic is reached only through `<domain>/api.py`.

### `build_app(profile, modules)` — the one composition root

Owns everything `app/main.py` does today, once:

```python
def build_app(profile: Profile, modules: Sequence[Module]) -> FastAPI:
    _validate(profile, modules)               # tier, secrets, capability checks (raises on mismatch)
    engine = _make_engine(profile)            # endpoint + role + read_only from the profile
    mcp = _new_mcp() if profile.mcp_enabled else None
    lifespan = _build_lifespan(profile, modules, engine, mcp)   # scheduler loop (composed), jobs, bot, ingest
    app = FastAPI(title=f"Monolith ({profile.tier.value})", lifespan=lifespan)
    app.state.engine = engine
    for m in modules:
        m.register(app)
        if mcp and m.register_mcp:
            m.register_mcp(mcp)               # aggregate all tooling onto one server
    if mcp:
        app.mount("/mcp", mcp.http_app(...))
    _add_health(app)
    _mount_static(app, profile)               # public vs private SvelteKit dist
    return app
```

`_validate` raises if a module's `tier` is incompatible with the profile, if `requires_secrets` is not a subset of `profile.allowed_secrets`, or if a module needs ClickHouse/MCP the profile disables. The scheduler loop started in `_build_lifespan` scans only the jobs registered by the composed modules; the public binary registers none and runs no loop.

The entrypoints become trivial and duplication-free:

```python
# app/main_private.py
app = build_app(PRIVATE_PROFILE, ALL_MODULES)
# app/main_public.py
app = build_app(PUBLIC_PROFILE, PUBLIC_MODULES)
```

## Data isolation (Layer 1, step one)

The substantive, load-bearing work. Each domain gets a Postgres schema and per-tier grants, landed against the primary before any code split.

- **Schemas:** `hikes`, `ships`, `stars`, `knowledge`, `home`, `chat`, `scheduler`. Existing tables move into their owning schema by `ALTER TABLE ... SET SCHEMA` (rename, not data copy), in Atlas migrations on the primary.
- **Grants:** `monolith_app` gets DML on all private + public schemas. `public_reader` (ADR 004) gets `SELECT` only on `hikes`, `ships`, `stars`, the `home` snapshot tables, and the `knowledge_public` view. No other grants; default-deny via `REVOKE ALL` then explicit grants.
- **Cross-domain reads become loud failures.** Once `knowledge` tables are out of the default schema, `chat`'s direct store access fails until it goes through `knowledge/api.py`. That is the point: the grant boundary surfaces every cross-domain coupling during step one.
- **Scheduler jobs** live in the `scheduler` schema, granted to `monolith_app` only (never `public_reader`). Open question 1 in the ADR: one shared jobs table vs. per-domain job tables; default is the shared `scheduler` schema for a simpler loop.

This step delivers real isolation inside the existing single binary, independent of the public/private split.

## Bazel layout (Layer 2)

```
projects/monolith/
  framework/BUILD            # py_library "framework" (Profile, Module, build_app), wide visibility
  hikes/BUILD                # "hikes_api" (wide vis); "hikes" internals (restricted), tags=["tier=public"]
  ships/BUILD                # "ships_api"; "ships" tags=["tier=public"]
  stars/BUILD                # "stars_api"; "stars" tags=["tier=public"]
  knowledge/BUILD            # "knowledge_core"; "knowledge_api"; "knowledge_public" (public); "knowledge_private" (private)
  home/BUILD                 # "home_public" (public); "home_private" (private)
  chat/BUILD                 # "chat_api"; "chat" tags=["tier=private"]  (deps may include //...:knowledge_api)
  scheduler/BUILD            # "scheduler" tags=["tier=private"]
  agent/BUILD                # "agent" tags=["tier=private"]
  app/BUILD                  # py_venv_binary "main_private" (deps = all); "main_public" (deps = public only)
```

Each domain's internals `py_library` restricts `visibility` to its own targets, its tests, and `//projects/monolith/app:*`; the `:_api` target is widely visible. Cross-domain deps may only reference `:_api`. `framework` and `shared` keep broad visibility.

### Boundary test

A `py_test`/`sh_test` runs `bazel cquery 'deps(//projects/monolith/app:main_public)'` and asserts no dep carries `tier=private`. Prototype the `tags` + cquery approach first; fall back to a `TierInfo` provider aspect if tags prove brittle (ADR open question 3).

## Testing both routes

- **Per-domain** (existing `py_test` per domain): unchanged, now against the isolated `py_library`.
- **`main_private_test.py`**: full route surface present; engine read-write.
- **`main_public_test.py`**: only public route prefixes; representative private routes 404; `app.state.engine` read-only; no `/mcp` mount; `build_app(PUBLIC_PROFILE, [a_private_module])` raises.
- **Boundary `cquery` test**: private libraries absent from the public binary's closure.
- **Data grant test**: as `public_reader`, asserting a private table/row is not selectable (ADR 004's mandated test, now per-schema).
- **`architecture_test.py`** extended: every domain exports a `MODULE` with a valid `Tier` and `schema`; cross-domain imports target only `:_api`; existing prefix checks retained.

## Migration sequence

Each step is independently shippable and CI-verified; the single private binary keeps working until the last step.

1. **Per-domain schemas + grants (Layer 1).** Atlas migrations moving each domain's tables into its schema, per-tier grants, `public_reader` scoped to public schemas/views. Repoint each domain's queries to its schema. Resolve every cross-domain read onto a temporary `api.py`. Ship inside the existing binary; this is the load-bearing change.
2. **Introduce the framework, behavior-preserving.** Add `framework/` (`Profile`, `Module`, `build_app`). Rewrite `app/main.py` as `build_app(PRIVATE_PROFILE, ALL_MODULES)`. One binary still; verify the rendered app, scheduler, and MCP surface are identical.
3. **Formalize `<domain>/api.py` + module objects.** Each domain exports `MODULE` and a stable `api.py`; extend `architecture_test.py`.
4. **Split per-domain `py_library` targets (Layer 2)** with tier tags and split `:_api` / internals visibility. Bulk of the `BUILD` churn; domain by domain.
5. **Split `knowledge`** into `knowledge_core` + `knowledge_public` + `knowledge_private`; resolve `home` into `home_public` / `home_private`.
6. **Add `main_public`.** New entrypoint, `py_venv_binary`, apko image, public SvelteKit dist. Add the `cquery` boundary test and `main_public_test`.
7. **Wire the public deployment (ADR 004 deliverables).** Point `PUBLIC_PROFILE` at `monolith-pg-ro` as `public_reader`; default-deny NetworkPolicy egress; SLO rollup job feeding `home_public`. Second Deployment in the chart for the public binary (one chart, two binaries).
8. **Cleanup.** Rename `app/main.py` -> `app/main_private.py` if desired; document the module + `api.py` pattern for new domains in `docs/services.md` / `docs/contributing.md`.

## Open questions (carried from the ADR)

1. Scheduler jobs: one shared `scheduler` schema vs. per-domain job tables.
2. Cross-domain MCP tool placement: owning module vs. a thin composition-level `agent` module depending on multiple `:_api` targets.
3. Tier as Bazel `tags` + cquery vs. a `TierInfo` provider aspect.
4. `home/observability` split shape.

## References

| Resource | Relevance |
| -------- | --------- |
| [ADR 010](../decisions/services/010-fastmonolith-modular-framework.md) | The decision this implements |
| [ADR 004](../decisions/security/004-public-read-only-service-isolation.md) | Security model and DB/replica/NetworkPolicy deliverables |
| `projects/monolith/app/main.py` | Current composition glue extracted into `build_app` |
| `projects/monolith/shared/scheduler.py` | SKIP-LOCKED loop composed per binary |
| `projects/monolith/app/mcp_app.py` | Single MCP instance the framework takes ownership of |
| `projects/monolith/app/architecture_test.py` | Convention enforcement extended for modules and `api.py` |
</content>
