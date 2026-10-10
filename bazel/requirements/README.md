# Python Requirements Management

Defines third-party package dependencies using layered pip-tools approach with uv for compilation.

## Architecture

```mermaid
flowchart TB
    subgraph "Source Files (.in)"
        PT[pyproject.toml<br/>runtime deps]
        TEST[test.in<br/>test-only deps]
        TOOLS[tools.in<br/>dev tools]
    end

    subgraph "Lock Files (.txt)"
        RT[runtime.txt<br/>pinned runtime]
        ALL[all.txt<br/>all deps combined]
        TOOL[tools.txt<br/>pinned tools]
    end

    subgraph "Bazel"
        PIP[@pip repository]
        TARGETS[py_library targets]
    end

    PT -->|uv pip compile| RT
    RT -->|constrain| TEST
    RT -->|constrain| TOOLS
    TEST -->|uv pip compile| ALL
    TOOLS -->|uv pip compile| TOOL
    ALL -->|pip.parse| PIP
    PIP --> TARGETS
```

## File Structure

| File                          | Purpose                                                  | Modified By      |
| ----------------------------- | -------------------------------------------------------- | ---------------- |
| `pyproject.toml#dependencies` | Runtime dependencies (loose constraints)                 | Developers       |
| `runtime.txt`                 | Pinned runtime dependencies                              | `uv pip compile` |
| `test.in`                     | Test-only dependencies (loose)                           | Developers       |
| `tools.in`                    | Dev tool dependencies (loose)                            | Developers       |
| `all.in`                      | Aggregator (references test.in + tools.in + runtime.txt) | Auto-generated   |
| `all.txt`                     | Pinned all dependencies                                  | `uv pip compile` |
| `tools.txt`                   | Pinned tool dependencies                                 | `uv pip compile` |

## Dependency Groups

### Runtime Dependencies

Defined in `pyproject.toml#dependencies`. Used by production services at runtime.

**Examples:**

- `fastapi~=0.109.0` - Web framework
- `pydantic~=2.5` - Data validation
- `opentelemetry-*` - Observability instrumentation
- `nats-py~=2.9` - NATS JetStream client

**Version Strategy:**

- Use compatible release clauses (`~=`) to allow patch updates
- Avoid exact pins (`==`) - let uv resolve compatible versions
- Only constrain when specific version ranges are incompatible

### Test Dependencies

Defined in `test.in`. Constrained against `runtime.txt` to prevent version conflicts.

**Examples:**

- `pytest` - Test framework

**Format:**

```bash
# test.in
-c runtime.txt  # Constrain to runtime versions
pytest
```

### Tool Dependencies

Defined in `tools.in`. Used for developer tasks and Bazel build tools, not tests or runtime.
The layer is currently empty (copier and semgrep were removed in #6913); `tools.txt` is
header-only until a tool is added.

**Format:**

```bash
# tools.in
some-tool>=1.5.0
```

## Workflow

### Adding a New Dependency

#### Runtime Dependency

1. Add to `pyproject.toml#dependencies`:

   ```toml
   dependencies = [
       # ...
       "new-package~=1.0",
   ]
   ```

2. Recompile lock files:

   ```bash
   bazel run //bazel/requirements:update
   ```

3. Update Bazel repository:

   ```bash
   bazel run @pnpm//:pnpm install
   ```

4. Reference in BUILD files:
   ```python
   py_library(
       name = "my_lib",
       deps = [
           "@pip//new_package",
       ],
   )
   ```

#### Test Dependency

1. Add to `test.in`:

   ```bash
   -c runtime.txt
   pytest
   new-test-tool~=2.0
   ```

2. Recompile and update as above.

#### Tool Dependency

1. Add to `tools.in`:

   ```bash
   new-tool>=1.5.0
   ```

2. Recompile and update as above.

### Updating Dependencies

#### Update All Dependencies

```bash
bazel run //bazel/requirements:update
```

This runs `uv pip compile` on all `.in` files and regenerates lock files.

#### Update Single Dependency

1. Modify version constraint in source file (pyproject.toml, test.in, or tools.in)
2. Run `bazel run //bazel/requirements:update`

#### Upgrade to Latest Versions

```bash
# Remove lock files to force fresh resolution
rm bazel/requirements/*.txt
bazel run //bazel/requirements:update
```

⚠️ **Warning:** This may introduce breaking changes. Always test thoroughly after upgrading.

### Troubleshooting

#### Dependency Conflict Error

**Symptom:**

```
ERROR: Cannot install package-a and package-b because these package versions have conflicting dependencies.
```

**Solution:**

1. Check which package introduced the conflict:

   ```bash
   uv pip tree
   ```

2. Adjust version constraints in pyproject.toml to find compatible range:

   ```toml
   # Before (conflict)
   dependencies = [
       "package-a~=2.0",
       "package-b~=1.5",
   ]

   # After (compatible)
   dependencies = [
       "package-a>=2.0,<2.5",  # Constrain upper bound
       "package-b~=1.5",
   ]
   ```

3. Recompile:
   ```bash
   bazel run //bazel/requirements:update
   ```

#### Bazel Can't Find Package

**Symptom:**

```
ERROR: no such package '@pip//missing_package'
```

**Solution:**

1. Verify package is in `all.txt`:

   ```bash
   grep missing-package bazel/requirements/all.txt
   ```

2. If missing, add to appropriate `.in` file and recompile.

3. Package names use underscores in Bazel, hyphens in PyPI:
   ```python
   # PyPI name: my-package
   # Bazel target: @pip//my_package
   deps = ["@pip//my_package"]
   ```

#### Lock File Out of Sync

**Symptom:**

```
ERROR: Failed to resolve dependencies. Try running 'bazel run //bazel/requirements:update'
```

**Solution:**

```bash
bazel run //bazel/requirements:update
bazel clean --expunge  # If update doesn't fix it
bazel run //bazel/requirements:update
```

#### uv Command Not Found

**Symptom:**

```
/bin/sh: uv: command not found
```

**Solution:**
Install uv globally (Bazel rules_uv handles this automatically, but for manual runs):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Layered Requirements Pattern

This repository uses the [pip-tools layered requirements workflow](https://pip-tools.readthedocs.io/en/stable/#workflow-for-layered-requirements):

1. **Runtime layer** (pyproject.toml → runtime.txt)
   - Minimal production dependencies
   - Acts as constraint for other layers

2. **Test layer** (test.in → all.txt)
   - Constrained by runtime.txt (`-c runtime.txt`)
   - Ensures test dependencies don't conflict with runtime

3. **Tools layer** (tools.in → tools.txt)
   - Standalone tools for development
   - Not included in all.txt (used separately)

**Why this pattern?**

- Prevents test dependencies from polluting runtime
- Ensures compatible versions across all layers
- Supports reproducible builds

## Integration with Bazel

Dependencies are referenced by `MODULE.bazel` via `pip.parse`:

```python
# MODULE.bazel
pip = use_extension("@rules_python//python/extensions:pip.bzl", "pip")
pip.parse(
    hub_name = "pip",
    requirements_lock = "//bazel/requirements:all.txt",
)
```

This creates the `@pip` repository with Bazel targets for each package.

**Usage in BUILD files:**

```python
py_library(
    name = "my_service",
    srcs = ["service.py"],
    deps = [
        "@pip//fastapi",
        "@pip//pydantic",
        "@pip//uvicorn",
    ],
)
```

## Version Constraints Reference

| Operator | Example      | Meaning                                |
| -------- | ------------ | -------------------------------------- |
| `~=`     | `~=1.4.2`    | `>=1.4.2, <1.5.0` (compatible release) |
| `>=`     | `>=1.0.0`    | Minimum version                        |
| `<`      | `<2.0.0`     | Upper bound (exclusive)                |
| `,`      | `>=1.0,<2.0` | Range (AND)                            |

**Recommended:** Use `~=` for most dependencies to allow patch updates while preventing breaking changes.

## Related Documentation

- [PEP 735 - Dependency Groups](https://peps.python.org/pep-0735/) (future support planned)
- [pip-tools layered requirements](https://pip-tools.readthedocs.io/en/stable/#workflow-for-layered-requirements)
- [uv documentation](https://github.com/astral-sh/uv)
- [rules_python pip.parse](https://github.com/bazelbuild/rules_python/blob/main/docs/pip.md)

## Examples

### Adding FastAPI Dependency

```bash
# 1. Add to pyproject.toml
echo 'fastapi~=0.109.0' >> pyproject.toml

# 2. Recompile
bazel run //bazel/requirements:update

# 3. Use in BUILD file
# py_library(..., deps = ["@pip//fastapi"])
```

### Adding Test Dependency

```bash
# 1. Edit test.in
cat >> bazel/requirements/test.in <<EOF
-c runtime.txt
pytest
pytest-asyncio~=0.21
EOF

# 2. Recompile
bazel run //bazel/requirements:update
```

### Checking Dependency Tree

```bash
# See why a package is included
bazel run @pip//:pip -- show <package-name>

# See all transitive dependencies
bazel query 'deps(@pip//fastapi)' --output=graph
```
