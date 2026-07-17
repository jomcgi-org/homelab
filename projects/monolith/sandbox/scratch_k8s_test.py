"""Tests for the EmberVM R5 scratch-k8s kubeconfig plumbing (client.py).

Hermetic: no network. Asserts that when SCRATCH_K8S_KUBECONFIG is set, the
kubeconfig is written to a guest temp file and KUBECONFIG points at it (so a guest
run_python snippet can kubectl/client-go), the value is repr-escaped (a multi-line
YAML doc with quotes survives intact), that an unset kubeconfig leaves the code
unchanged, and that the kubeconfig and the scratch-postgres DSN compose (both
injected when both are set). Also asserts the injected preamble reaches the POSTed
payload end to end via the same fake-client seam client_test uses.
"""

from __future__ import annotations

import httpx
import pytest

from sandbox import client

# A representative kubeconfig: multi-line YAML with a quoted, TLS-skip server and a
# bearer token (the stable EMBER_GROUP_SECRET). The exact shape the chart builds.
_KUBECONFIG = (
    "apiVersion: v1\n"
    "kind: Config\n"
    "clusters:\n"
    "- name: scratch-k8s\n"
    "  cluster:\n"
    "    server: https://embervm-embervm-serving.embervm.svc:5410\n"
    "    insecure-skip-tls-verify: true\n"
    "users:\n"
    "- name: ember\n"
    "  user:\n"
    '    token: "s3cr3t-group-secret"\n'
    "contexts:\n"
    "- name: scratch-k8s\n"
    "  context:\n"
    "    cluster: scratch-k8s\n"
    "    user: ember\n"
    "current-context: scratch-k8s\n"
)


class _Resp:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {"stdout": "", "exit_code": 0}


class _FakeClient:
    posts: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.posts.append({"url": url, "json": json})
        return _Resp()


def _isolate_features(monkeypatch, *, dsn="", kubeconfig=""):
    """Pin both scratch features so a test controls exactly what is injected."""
    monkeypatch.setattr(client, "SCRATCH_POSTGRES_DSN", dsn)
    monkeypatch.setattr(client, "SCRATCH_K8S_KUBECONFIG", kubeconfig)


def test_kubeconfig_unset_returns_code_unchanged(monkeypatch):
    _isolate_features(monkeypatch)
    code = "print('hi')"
    assert client._with_scratch_env(code) == code


def test_kubeconfig_injects_env_and_tempfile(monkeypatch):
    _isolate_features(monkeypatch, kubeconfig=_KUBECONFIG)
    out = client._with_scratch_env("print('hi')")
    # The user code is preserved and KUBECONFIG is set in the guest process env
    # (pointing at a written temp file) before it runs.
    assert out.endswith("print('hi')")
    assert "os.environ['KUBECONFIG']" in out.replace("_os", "os")
    # The kubeconfig content is written to the temp file.
    assert "NamedTemporaryFile" in out
    # The full kubeconfig YAML rides along as a repr literal.
    assert repr(_KUBECONFIG) in out


def test_kubeconfig_repr_escapes_multiline_and_quotes(monkeypatch):
    # The multi-line YAML with embedded double-quotes must not break out of the
    # literal; repr() escapes the newlines and quotes.
    _isolate_features(monkeypatch, kubeconfig=_KUBECONFIG)
    out = client._with_scratch_env("pass")
    assert repr(_KUBECONFIG) in out
    # A raw unescaped newline from the kubeconfig must NOT appear before the user
    # code (it would if the value were interpolated rather than repr'd).
    preamble = out[: out.index("pass")]
    assert "current-context: scratch-k8s\n" not in preamble


def test_kubeconfig_and_dsn_compose(monkeypatch):
    dsn = "postgresql://postgres:pw@embervm-embervm-serving.embervm.svc:5400/scratch"
    _isolate_features(monkeypatch, dsn=dsn, kubeconfig=_KUBECONFIG)
    out = client._with_scratch_env("print('hi')")
    # Both features inject; a single shared preamble sets both.
    assert dsn in out
    assert repr(_KUBECONFIG) in out
    assert "os.environ['SCRATCH_POSTGRES_DSN']" in out.replace("_os", "os")
    assert "os.environ['KUBECONFIG']" in out.replace("_os", "os")
    assert out.endswith("print('hi')")


@pytest.mark.asyncio
async def test_kubeconfig_reaches_posted_payload(monkeypatch):
    _FakeClient.posts = []
    _isolate_features(monkeypatch, kubeconfig=_KUBECONFIG)
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "FC_INVOKE_URL", "http://fc")
    monkeypatch.setattr(client, "SANDBOX_DISPATCH", "fc-invoke")

    await client.run_python_in_sandbox("print('hi')")

    assert _FakeClient.posts, "expected a POST"
    posted_code = _FakeClient.posts[0]["json"]["code"]
    assert repr(_KUBECONFIG) in posted_code
    assert posted_code.endswith("print('hi')")


@pytest.mark.asyncio
async def test_no_kubeconfig_leaves_payload_code_untouched(monkeypatch):
    _FakeClient.posts = []
    _isolate_features(monkeypatch)
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "FC_INVOKE_URL", "http://fc")
    monkeypatch.setattr(client, "SANDBOX_DISPATCH", "fc-invoke")

    await client.run_python_in_sandbox("print('hi')")

    posted_code = _FakeClient.posts[0]["json"]["code"]
    assert posted_code == "print('hi')"


# httpx is imported so the fake-client seam matches client_test's dependency set.
_ = httpx
