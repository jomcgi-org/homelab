"""Stop a Hindsight sync before it replaces a working pod with one that cannot start.

Runs as an ArgoCD Sync hook in wave -1: after the 1Password items (wave -2),
before the Deployment (wave 0). It fails the sync, and leaves the running pod
alone, when:

- the OpenRouter key is empty, OpenRouter rejects the key or model, or the
  model returns another dimension than EMBEDDINGS_DIMENSIONS;
- the decision model (System One at DECISION_MODEL_BASE_URL) refuses or does
  not answer. Recall has no local fallback, so the pod would serve failures.
  Hindsight cannot send OpenRouter's data policy with decision calls, so the
  model must have exactly one OpenRouter endpoint, and it must answer when the
  request requires zero data retention. OpenRouter's preferences cannot pin
  one variant of a provider (`only` and `ignore` match every variant), so a
  second endpoint stops the sync for a person to check;
- stored rows have another dimension than EMBEDDINGS_DIMENSIONS. Hindsight
  resizes an empty vector column at startup but refuses one with rows.

Uses the image's Python, its asyncpg and the standard library.
"""

import asyncio
import json
import os
import urllib.error
import urllib.request

TABLES = ("memory_units", "mental_models")
EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
# Every schema: a tenant extension can keep each tenant's tables in its own,
# and `hindsight-admin reembed --all-schemas` covers them all.
EMBEDDING_COLUMNS = (
    "select n.nspname, c.relname, format_type(a.atttypid, a.atttypmod) "
    "from pg_attribute a "
    "join pg_class c on c.oid = a.attrelid "
    "join pg_namespace n on n.oid = c.relnamespace "
    "where c.relkind = 'r' and c.relname = any($1::text[]) "
    "and a.attname = 'embedding' and not a.attisdropped "
    "order by 1, 2"
)


def model_dimension(key, model, extra_body):
    """Embed one string through OpenRouter, the way Hindsight will; its length."""
    body = {"model": model, "input": ["hindsight preflight"], **extra_body}
    req = urllib.request.Request(
        EMBEDDINGS_URL,
        method="POST",
        data=json.dumps({**body, "encoding_format": "float"}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return len(json.load(resp)["data"][0]["embedding"])
    except urllib.error.HTTPError as e:
        raise SystemExit(f"OpenRouter refused the key or model {model}: HTTP {e.code}")


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{}/endpoints"
# OpenRouter routes such a request only to an endpoint that keeps no data.
ZERO_RETENTION = {"zdr": True, "data_collection": "deny", "allow_fallbacks": False}


def decision_endpoints(key, model):
    """The provider tag of every endpoint OpenRouter lists for ``model``."""
    req = urllib.request.Request(
        ENDPOINTS_URL.format(model), headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            endpoints = json.load(resp)["data"]["endpoints"]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"OpenRouter lists no endpoints for {model}: HTTP {e.code}")
    return [e["tag"] for e in endpoints]


def decision_answers(key, base_url, model, provider=None):
    """Ask the decision model one question, as Hindsight's System One client does."""
    body = {
        "model": model,
        "state": "Question: Is this a preflight check?",
        "questions": {"q": {"type": "noul", "instructions": "This is a preflight."}},
    }
    if provider is not None:
        body["provider"] = provider
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/systemone",
        method="POST",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            answer = (json.load(resp).get("answers") or {}).get("q") or {}
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"The decision model {model} refused{via(provider)}: HTTP {e.code}"
        )
    if not isinstance(answer.get("noul"), (int, float)):
        raise SystemExit(f"The decision model {model} gave no answer{via(provider)}")


def via(provider):
    return " with zero data retention" if provider else ""


async def stored(conn):
    """{schema.table: (column type, rows with a vector)} in every schema."""
    out = {}
    for schema, table, column in await conn.fetch(EMBEDDING_COLUMNS, list(TABLES)):
        rows = await conn.fetchval(
            f"select count(*) from {quoted(schema)}.{quoted(table)} "
            "where embedding is not null"
        )
        out[f"{schema}.{table}"] = (column, rows)
    return out


def refused(columns, dimension):
    """The tables Hindsight would refuse to start on with ``dimension``."""
    want = f"vector({dimension})"
    return [
        f"{table} holds {rows} rows as {column}"
        for table, (column, rows) in columns.items()
        if column != want and rows
    ]


async def database(url):
    import asyncpg  # The image's; the unit tests replace database().

    conn = await asyncpg.connect(url)
    try:
        return await stored(conn)
    finally:
        await conn.close()


def main():
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("Secret hindsight-openrouter has an empty api-key")
    model = os.environ["EMBEDDINGS_MODEL"]
    dimension = int(os.environ["EMBEDDINGS_DIMENSIONS"])
    extra = json.loads(os.environ.get("EMBEDDINGS_EXTRA_BODY") or "{}")
    returned = model_dimension(key, model, extra)
    if returned != dimension:
        raise SystemExit(f"{model} returns {returned} dimensions, not {dimension}")
    decision = os.environ["DECISION_MODEL"]
    endpoints = decision_endpoints(key, decision)
    if len(endpoints) != 1:
        raise SystemExit(
            f"{decision} has OpenRouter endpoints {endpoints}, not exactly one: "
            "check each one's data policy, then update the preflight"
        )
    base_url = os.environ["DECISION_MODEL_BASE_URL"]
    decision_answers(key, base_url, decision, ZERO_RETENTION)
    columns = asyncio.run(database(os.environ["DATABASE_URL"]))
    for table, (column, rows) in columns.items():
        print(f"{table}: {column}, {rows} rows with a vector")
    problems = refused(columns, dimension)
    if problems:
        raise SystemExit(
            f"{model} returns {dimension} dimensions, but "
            + "; ".join(problems)
            + ". Re-embed first: projects/factory/ARCHITECTURE.md, Memory."
        )
    print(f"preflight passed: {model}, {dimension} dimensions")


if __name__ == "__main__":
    main()
