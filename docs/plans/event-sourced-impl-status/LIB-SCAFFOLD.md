# LIB-SCAFFOLD — projects/lakehouse scaffold + pip dependencies

**Unit:** LIB-SCAFFOLD (orchestrator-authored prerequisite for Wavefront 2)
**Classification:** [manual-review] — touches shared files (`pyproject.toml`, regenerated `bazel/requirements/*.txt`).

## Why this unit exists (not in the original plan)

Wavefront-0 discovery established the code home as a standalone `projects/lakehouse/`
project (gate-ratified Option A). Two shared touchpoints would make the five
parallel Wavefront-2 library units collide, so they are created **once, serialized,
here**, before the fan-out:

1. **The top-level `projects/lakehouse/BUILD` + `__init__.py`** — gazelle creates a
   top-level BUILD for a standalone project (as it does for `stargazer`). Created
   here so sub-package units only ever add their own subdir BUILD.
2. **The pip requirements lock** — the new libraries need `temporalio`, `pyiceberg`
   (+`pyarrow`), `duckdb`. Adding pip deps edits `pyproject.toml` and regenerates the
   ~380 KB `bazel/requirements/{runtime,all}.txt` lock. Three units each editing that
   would conflict catastrophically, so **all** new deps are added in this one step.

## What shipped

- `pyproject.toml#dependencies`: added `temporalio>=1.8`, `pyiceberg>=0.8`,
  `pyarrow>=17`, `duckdb>=1.1`. (`nats-py~=2.9`, `boto3>=1.34`, `pydantic~=2.5`
  already present — reused, not re-added.)
- Regenerated `bazel/requirements/runtime.txt` + `all.txt` via the requirements
  update hook (uv compile).
- `projects/lakehouse/__init__.py` — package docstring describing the stack.
- `projects/lakehouse/BUILD` — `py_library(name="lakehouse")` + semgrep_test
  (stargazer pattern; no image targets yet — those land in Wavefront 3).
- `projects/lakehouse/README.md` — layout + conventions reference.

## Conventions established (read before any Wavefront-2 unit)

- **Import style:** workspace-root absolute — `from projects.lakehouse.events import ...`
  (the standalone-project convention; `stargazer` uses `from projects.stargazer.backend...`).
  NOT monolith's `imports=["."]`. Each sub-package needs an `__init__.py`.
- **BUILD:** gazelle-managed per sub-directory. Each unit adds only
  `projects/lakehouse/<pkg>/` + its files; run `format` so gazelle generates the
  sub-package BUILD (py_library + semgrep_test). Cross-package deps are Bazel labels
  (e.g. `//projects/lakehouse/events`), never file edits to a sibling.
- **pip deps:** reference `@pip//temporalio`, `@pip//pyiceberg`, `@pip//pyarrow`,
  `@pip//duckdb`, `@pip//nats_py`, `@pip//boto3`, `@pip//pydantic` (underscored Bazel
  names). Do NOT add more deps without serializing through another scaffold-style step.

## Deviations / notes

- Loose version floors (per pyproject.toml guidance: avoid `==`, let uv resolve).
  If uv surfaces a protobuf/grpcio conflict (temporalio vs opentelemetry-proto), it is
  resolved by an entry in `bazel/requirements/overrides.txt` (documented there).
