# FastMonolith Framework — Implementation Design

**Date:** 2026-06-15 (revised 2026-06-16)
**ADR:** [services/010-fastmonolith-modular-framework](../decisions/services/010-fastmonolith-modular-framework.md)
**Builds on:** [security/004-public-read-only-service-isolation](../decisions/security/004-public-read-only-service-isolation.md)

---

## Scope

Extract a small in-repo framework (`projects/monolith/framework/`) that composes the monolith into two per-tier binaries from data-isolated domain modules, and use it to ship ADR 004's public/private split. The design rests on two boundaries plus a thin composition layer:

1. **Data isolation**: per-domain Postgres schema (the decoupling; step one).
2. **Runtime security context**: the public binary runs with no secrets and a read-only public-only DB grant (the public/private boundary). Code crossover between tiers is tolerated, so there is no build-graph exclusion test.
3. **Composition**: one thin `build_app(profile, modules)` shared by `main_public` and `main_private`; scheduler and MCP composed per binary; `<domain>/api.py` as the endpoint-shaped cross-domain contract.

Out of scope: per-domain production charts, separate databases per domain, any change to ADR 004's replica / NetworkPolicy / rollup decisions (consumed as-is).

## The framework surface

`framework/` is intentionally tiny. Three concepts.

### `Profile`: what a binary's runtime can do

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
    allowed_secrets: frozenset[str]   # public: empty
    clickhouse_enabled: bool
    mcp_enabled: bool

PRIVATE_PROFILE = Profile(Tier.PRIVATE, "DATABASE_URL", "monolith_app", False, ALL_SECRETS, True, True)
PUBLIC_PROFILE  = Profile(Tier.PUBLIC,  "DATABASE_RO_URL", "public_reader", True, frozenset(), False, False)
```

The profile is the runtime boundary: `PUBLIC_PROFILE` carries no secrets and a read-only role. The deployment reinforces it (the public pod simply does not mount the secret env / OnePasswordItems), so even linked private code has nothing to use.

### `Module`: what a domain provides

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

Each domain exports `MODULE = Module(...)`, reusing its existing `register` / `on_startup_jobs`, so the wrapper is thin. Cross-domain logic is reached only through `<domain>/api.py`.

### `build_app(profile, modules)`: the one composition root

Owns everything `app/main.py` does today, once:

```python
def build_app(profile: Profile, modules: Sequence[Module]) -> FastAPI:
    _validate(profile, modules)               # secrets/capability checks (raises on mismatch)
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

`_validate` raises if `requires_secrets` is not a subset of `profile.allowed_secrets`, or if a module needs ClickHouse/MCP the profile disables, so a public binary fails fast rather than silently shipping a path that needs a secret it does not have. The scheduler loop in `_build_lifespan` scans only the job tables of the composed modules; the public binary registers none and runs no loop.

The entrypoints are trivial and duplication-free:

```python
# app/main_private.py
app = build_app(PRIVATE_PROFILE, ALL_MODULES)
# app/main_public.py
app = build_app(PUBLIC_PROFILE, PUBLIC_MODULES)
```

`PUBLIC_MODULES` composes only public modules, so private routes are never registered on the public binary even though shared/private code may be transitively linked.

## Boundary 1: data isolation (step one)

The substantive, decoupling work. Each domain gets a Postgres schema and per-tier grants, landed against the primary before any code split.

- **Schemas:** `hikes`, `ships`, `stars`, `knowledge`, `home`, `chat`. Existing tables move into their owning schema by `ALTER TABLE ... SET SCHEMA` (rename, not data copy), in Atlas migrations on the primary.
- **Per-domain scheduler tables:** each domain that schedules work owns its `*_jobs` table in its own schema (not one global jobs table). The shared loop scans the composed domains' job tables.
- **Grants:** `monolith_app` gets DML on all schemas. `public_reader` (ADR 004) gets `SELECT` only on `hikes`, `ships`, `stars`, the `home` snapshot tables, and the `knowledge_public` view. Default-deny via `REVOKE ALL` then explicit grants. No scheduler tables for `public_reader`.
- **Cross-domain reads become loud failures.** Once `knowledge` tables leave the default schema, `chat`'s direct store access fails until it goes through `knowledge/api.py`. That is the point: the grant boundary surfaces every cross-domain coupling here, at step one.

This delivers real isolation inside the existing single binary, independent of the public/private split.

## Boundary 2: runtime security context

The public/private separation lives in the deployment, not the build:

- The public Deployment injects **no secrets** (no `DISCORD_BOT_TOKEN`, `GITHUB_TOKEN`, `CLICKHOUSE_*`, `VAULT_*`, etc.) and points `DATABASE_RO_URL` at `monolith-pg-ro` as `public_reader`.
- `PUBLIC_PROFILE` makes the engine read-only and `allowed_secrets` empty, so any module needing a secret fails `build_app` validation.
- Therefore code crossover is safe: shared or private code linked into the public binary is inert without tokens and cannot read private rows without the grant. **No `cquery` exclusion test is built**: artifact contents are not the boundary.

## `<domain>/api.py`: the cross-domain contract

The only legal cross-domain seam, designed to become a network API:

- Inputs and outputs are serializable (Pydantic models / plain data); no `Session` or ORM objects cross the boundary.
- Signatures are identical whether called in-process (today, for convenience / saving a hop) or over HTTP (after a future domain extraction), so `api.py` is the cut point for making a domain its own service.
- Covers every cross-domain need: `chat` -> `knowledge`, cross-domain MCP tools, etc. Cross-domain access is the exception, not the norm.

## Bazel layout

Per-domain `py_library` targets for build/test modularity (not a security wall):

```
projects/monolith/
  framework/BUILD            # py_library "framework" (Profile, Module, build_app), wide visibility
  hikes/BUILD                # "hikes_api" (wide vis); "hikes" internals
  ships/BUILD ; stars/BUILD  # same shape
  knowledge/BUILD            # "knowledge_core"; "knowledge_api"; "knowledge_public"; "knowledge_private"
  home/BUILD                 # "home_public"; "home_private"
  chat/BUILD                 # "chat_api"; "chat"  (deps may include //...:knowledge_api)
  scheduler/BUILD ; agent/BUILD
  app/BUILD                  # py_venv_binary "main_private" (deps = all); "main_public" (deps = public only)
```

Cross-domain deps reference only `:_api`. Visibility keeps coupling honest (a domain's internals are not widely visible), but it is hygiene, not the security control, so it is not backed by a build-graph exclusion test.

## Testing both routes

- **Per-domain** (existing `py_test` per domain): unchanged, now against the isolated `py_library`.
- **`main_private_test.py`**: full route surface present; engine read-write.
- **`main_public_test.py`**: only public route prefixes; representative private routes 404; `app.state.engine` read-only; no `/mcp` mount; `build_app(PUBLIC_PROFILE, [module_requiring_a_secret])` raises.
- **Data grant test**: as `public_reader`, assert a private table/row is not selectable and writes are rejected (ADR 004's mandated test, now per-schema). This is the test that proves the boundary.
- **`architecture_test.py`** extended: every domain exports a `MODULE` with a valid `Tier` and `schema`; cross-domain imports target only `:_api`; `api.py` signatures are serializable; existing prefix checks retained.

## Migration sequence

Each step is independently shippable and CI-verified; the single private binary keeps working until the last step.

1. **Per-domain schemas + grants + per-domain scheduler tables (Boundary 1).** Atlas migrations moving each domain's tables into its schema, per-tier grants, `public_reader` scoped to public schemas/views. Repoint queries. Resolve every cross-domain read onto an `api.py` function. Ship inside the existing binary; this is the load-bearing change.
2. **Introduce the framework, behavior-preserving.** Add `framework/` (`Profile`, `Module`, `build_app`). Rewrite `app/main.py` as `build_app(PRIVATE_PROFILE, ALL_MODULES)`. One binary still; verify the rendered app, scheduler, and MCP surface are identical.
3. **Formalize `<domain>/api.py` + module objects.** Each domain exports `MODULE` and a stable, serializable `api.py`; extend `architecture_test.py`.
4. **Split per-domain `py_library` targets** with `:_api` / internals visibility. Domain by domain.
5. **Split `knowledge`** into `knowledge_core` + `knowledge_public` + `knowledge_private`; resolve `home` into `home_public` / `home_private`.
6. **Add `main_public`.** New entrypoint, `py_venv_binary`, apko image, public SvelteKit dist. Add `main_public_test`.
7. **Wire the public deployment (ADR 004 deliverables).** Public Deployment with no secrets; `PUBLIC_PROFILE` at `monolith-pg-ro` as `public_reader`; default-deny NetworkPolicy egress; SLO rollup job feeding `home_public`. Second Deployment in the chart (one chart, two binaries).
8. **Cleanup.** Rename `app/main.py` -> `app/main_private.py` if desired; document the module + `api.py` pattern in `docs/services.md` / `docs/contributing.md`.

## Open questions (carried from the ADR)

1. `home/observability` split shape (full `_core`/`_public`/`_private` vs. a thin `home_public` reading only snapshot tables).
2. Shared reference-data ownership: any table read by several domains needs an owning schema and an `api.py`; inventory during step one.
3. `api.py` enforcement: architecture test vs. lint vs. convention plus review.

## References

| Resource | Relevance |
| -------- | --------- |
| [ADR 010](../decisions/services/010-fastmonolith-modular-framework.md) | The decision this implements |
| [ADR 004](../decisions/security/004-public-read-only-service-isolation.md) | Security model and DB/replica/NetworkPolicy deliverables |
| `projects/monolith/app/main.py` | Current composition glue extracted into `build_app` |
| `projects/monolith/shared/scheduler.py` | SKIP-LOCKED loop composed per binary |
| `projects/monolith/app/mcp_app.py` | Single MCP instance the framework takes ownership of |
| `projects/monolith/app/architecture_test.py` | Convention enforcement extended for modules and `api.py` |
