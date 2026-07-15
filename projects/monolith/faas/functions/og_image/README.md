# og-image: the first registered EmberVM FaaS function

`og-image` renders an Open Graph share card (1200x630 PNG) from `title` and
`subtitle` query params. It is the dogfood consumer for the FaaS framework (ADR
agents/045, plan Task 12): it proves the full registration path (upload -> CR ->
build -> smoke-gate -> visible) with **no hand-authored CR**, and its binary PNG
response exercises the base64 body path plus the guest-header `Content-Type`
fidelity landed in PR-A.

## Files

- `app.py` -- the guest handler (`app.handle`). Pure Pillow, restore-safe (no
  wall-clock, no entropy): output is a pure function of `(title, subtitle)`. Its
  only non-stdlib import is `PIL`, which is in the runtime base's baked subset.
  Guest code, but globbed into `:monolith_backend` so the CI test can import it.
- `register.py` -- idempotent registration CLI + the deterministic zip bundler.
- `og_image_test.py` -- real-render tests (PNG magic bytes, dimensions) and the
  zip-determinism test. Runs in CI (`//projects/monolith:faas_og_image_test`).

## Invocation contract

`GET /functions/og-image?title=<t>&subtitle=<s>` -> `200 image/png`.

The invocation router (`faas/invoke_router.py`) appends the caller's raw query to
the guest path, so the shim parses `title`/`subtitle` into
`event.queryStringParameters`. Missing params fall back to a default title and an
empty subtitle. Inputs are clamped (`title` 120 chars, `subtitle` 200) and
ellipsised if longer.

## Registration (idempotent by zip sha)

`register.py` builds a **deterministic** zip of `app.py` (fixed member name,
`2000-01-01` timestamp, mode `0644`, deflate), so the sha256 depends only on
`app.py`'s bytes. Idempotency is a server property: `POST /api/functions`
short-circuits to a no-op when an identical `(zip sha256, handler, runtime,
visibility)` is already registered and smoked (see `faas/router.py`), so
re-running on an unchanged `app.py` does no upload, no CR churn, and no VM smoke.
The script therefore just always POSTs and reports whether the server registered
or no-op'd.

The `/api/functions` endpoint is private (behind Cloudflare Access), so reach it
via a port-forward that bypasses the edge (mirrors the echo live-verify recipe):

```bash
# From a checkout, against the running monolith (namespace as deployed):
kubectl -n <ns> port-forward deploy/monolith 8000:8000 >/dev/null 2>&1 &
PF=$!
FAAS_API_BASE=http://localhost:8000 \
  python3 projects/monolith/faas/functions/og_image/register.py
kill "$PF"
```

`register.py` needs `httpx` on the path (it is a monolith dependency; run it with
the monolith venv/toolchain, or in-cluster from the monolith image where the
endpoint is reachable directly without a port-forward). Exit status is 0 on a
fresh registration OR an idempotent no-op, non-zero on any failure (the server's
error detail is printed).

The natural CI/deploy home for this is an in-cluster reconcile step using the
monolith image (which carries both the code and `httpx`, and reaches
`/api/functions` directly): BuildBuddy RBE cannot reach the private in-cluster
endpoint, so this is not wired as a BuildBuddy action.

## Live verification (Task 12 acceptance)

After registering (above), invoke it on the private tier through the same
port-forward and confirm a real PNG comes back:

```bash
kubectl -n <ns> port-forward deploy/monolith 8000:8000 >/dev/null 2>&1 &
PF=$!
curl -sS "http://localhost:8000/functions/og-image?title=Deploys&subtitle=shipped" \
  -o /tmp/og.png -D -
file /tmp/og.png            # expect: PNG image data, 1200 x 630
kill "$PF"
```

`Content-Type: image/png` in the response headers confirms the guest-header
fidelity relay. Re-running `register.py` with an unchanged `app.py` must print
`unchanged (no-op)` and leave the function serving.
