defmodule Embervm.RouterTest do
  @moduledoc """
  Request tests for the submit API, driving the LIVE Bandit instance the
  application boots (over the Embervm.Finch pool on loopback) with an injected
  fake authenticator, so no live Kubernetes API server is needed. `async: false`
  because these mutate global application env (the authenticator + sync knobs)
  and share the one running control-plane instance; unique workloads and
  idempotency keys keep tasks from colliding across tests.
  """
  use ExUnit.Case, async: false

  alias Embervm.TaskStore

  @allowed "system:serviceaccount:embervm:embervm"

  defmodule FakeAuth do
    @allowed "system:serviceaccount:embervm:embervm"
    def authenticate("good"), do: {:ok, @allowed}
    def authenticate("good2"), do: {:ok, "principal-2"}
    def authenticate("forbidden"), do: {:error, :forbidden}
    def authenticate(_), do: {:error, :unauthenticated}
  end

  # Fakes for the R2 session routes: the router resolves the session manager/store
  # from app-env (the :session_manager / :session_store_mod keys), so a request test
  # can drive the HTTP surface, and especially the SESSION-TOKEN auth boundary,
  # without a live daemon or the supervised SessionManager.
  defmodule FakeSessionManager do
    def create(_srv, "wl-ok", _principal),
      do: {:ok, %{session_id: "s-live", token: "sess-token-live", expires_at: 9_000_000, base_digest: "sha256:x", state: :running}}

    def create(_srv, "wl-cap", _principal), do: {:error, {:denied, :session_cap}}
    def create(_srv, "wl-task", _principal), do: {:error, {:denied, :not_session_class}}
    def create(_srv, _wl, _principal), do: {:error, {:denied, :unknown_workload}}

    def invoke(_srv, "s-live", _req), do: {:ok, %{status_code: 200, headers: %{"content-type" => "text/plain"}, body: "echoed"}}
    def invoke(_srv, "s-queue", _req), do: {:error, :queue_full}
    def invoke(_srv, _id, _req), do: {:error, :not_found}

    def destroy(_srv, "s-live"), do: {:ok, :destroyed}
    def destroy(_srv, _id), do: {:error, :not_found}
  end

  defmodule FakeSessionStore do
    # s-live's token is "sess-token-live"; any other token is unauthorized.
    def verify_token(_srv, "s-live", "sess-token-live"), do: {:ok, %{session_id: "s-live"}}
    def verify_token(_srv, "s-live", _), do: {:error, :unauthorized}
    def verify_token(_srv, "s-queue", "sess-token-queue"), do: {:ok, %{session_id: "s-queue"}}
    def verify_token(_srv, "s-term", "sess-token-term"), do: {:error, :terminal}
    def verify_token(_srv, _id, _token), do: {:error, :not_found}

    def get(_srv, "s-live"),
      do: {:ok, %{session_id: "s-live", workload: "wl-ok", principal: "p", state: :running, generation: 0, base_digest: "sha256:x", created_at: 1, last_invoke_at: nil, expires_at: 9_000_000, updated_at: 1, terminal_reason: nil}}

    def get(_srv, "s-term"),
      do: {:ok, %{session_id: "s-term", workload: "wl-ok", principal: "p", state: :destroyed, generation: 0, base_digest: "sha256:x", created_at: 1, last_invoke_at: nil, expires_at: 9_000_000, updated_at: 1, terminal_reason: "destroyed"}}

    def get(_srv, _id), do: :error

    def list(_srv, "wl-ok", _opts),
      do: {:ok, %{items: [], total: 0, limit: 50, offset: 0}}

    def list(_srv, _wl, _opts), do: {:ok, %{items: [], total: 0, limit: 50, offset: 0}}
  end

  setup do
    Application.put_env(:embervm, :authenticator, FakeAuth)

    on_exit(fn ->
      Application.delete_env(:embervm, :authenticator)
      Application.delete_env(:embervm, :sync_park_cap)
      Application.delete_env(:embervm, :sync_timeout_ms)
      Application.delete_env(:embervm, :quota)
      Application.delete_env(:embervm, :usage_admins)
      Application.delete_env(:embervm, :session_manager)
      Application.delete_env(:embervm, :session_store_mod)
    end)

    :ok
  end

  defp with_session_fakes do
    Application.put_env(:embervm, :session_manager, FakeSessionManager)
    Application.put_env(:embervm, :session_store_mod, FakeSessionStore)
  end

  defp unique(prefix), do: "#{prefix}-#{System.unique_integer([:positive, :monotonic])}"

  defp req(method, path, headers \\ [], body \\ "") do
    {:ok, resp} =
      Finch.build(method, "http://127.0.0.1:8080#{path}", headers, body)
      |> Finch.request(Embervm.Finch)

    resp
  end

  defp auth(token), do: [{"authorization", "Bearer " <> token}]

  defp json(body), do: :json.decode(body)

  # -- auth ------------------------------------------------------------------

  test "a /v1 request without a token is 401" do
    resp = req(:post, "/v1/workloads/#{unique("wl")}/tasks")
    assert resp.status == 401
  end

  test "an unauthenticated token is 401" do
    resp = req(:post, "/v1/workloads/#{unique("wl")}/tasks", auth("nope"), "x")
    assert resp.status == 401
  end

  test "an authenticated but non-allow-listed token is 403" do
    resp = req(:post, "/v1/workloads/#{unique("wl")}/tasks", auth("forbidden"), "x")
    assert resp.status == 403
  end

  test "/healthz needs no auth" do
    resp = req(:get, "/healthz")
    assert resp.status == 200
    assert resp.body == "ok"
  end

  test "/v1/nodes needs auth and returns a node + dispatch snapshot" do
    assert req(:get, "/v1/nodes").status == 401

    resp = req(:get, "/v1/nodes", auth("good"))
    assert resp.status == 200
    body = json(resp.body)
    # Read-only operational introspection: both sections present and shaped, even
    # with no node wired in the test app (nodes empty, dispatch snapshot present).
    assert is_list(body["nodes"])
    assert is_map(body["dispatch"])
  end

  # -- submit ----------------------------------------------------------------

  test "async submit returns 202 and creates a queued task backed by the op-log" do
    wl = unique("wl")
    resp = req(:post, "/v1/workloads/#{wl}/tasks", auth("good"), "source-file-bytes")

    assert resp.status == 202
    body = json(resp.body)
    assert body["state"] == "queued"
    task_id = body["task_id"]
    assert is_binary(task_id)

    # The queued task is durable (write-through op-log projection surfaced via
    # the ETS hot set): GET reflects it.
    got = req(:get, "/v1/tasks/#{task_id}", auth("good"))
    assert got.status == 200
    view = json(got.body)
    assert view["state"] == "queued"
    assert view["workload"] == wl
    assert view["attempt"] == 1
  end

  test "a submit without X-Ember-Guest-Path stores NO path, so the workload invokePath applies" do
    wl = unique("wl")
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", auth("good"), "x").body)["task_id"]

    {:ok, env} = TaskStore.get_request(task_id)
    # No "path" key: the dispatcher's `req_env["path"] || invoke_path` then falls
    # back to the workload's invokePath (e.g. /invoke), not a hard-coded "/".
    refute Map.has_key?(env, "path")
  end

  test "X-Ember-Guest-Path is recorded and overrides the workload invokePath" do
    wl = unique("wl")
    headers = auth("good") ++ [{"x-ember-guest-path", "/custom"}]
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", headers, "x").body)["task_id"]

    {:ok, env} = TaskStore.get_request(task_id)
    assert env["path"] == "/custom"
  end

  test "Idempotency-Key dedupes a resubmit to the same task" do
    wl = unique("wl")
    key = unique("idem")
    headers = auth("good") ++ [{"idempotency-key", key}]

    a = req(:post, "/v1/workloads/#{wl}/tasks", headers, "body")
    b = req(:post, "/v1/workloads/#{wl}/tasks", headers, "body")

    assert a.status == 202
    assert b.status == 202
    assert json(a.body)["task_id"] == json(b.body)["task_id"]
  end

  test "a body over 8 MiB is rejected 413" do
    # An over-cap POST is answered 413 BEFORE the server reads the whole body,
    # which leaves the HTTP/1.1 connection with an undrained tail. Reusing that
    # pooled connection for a later request would misframe and hang it, so this
    # abnormal request runs against its OWN throwaway Finch pool that no other
    # test shares; the app's shared Embervm.Finch pool is never poisoned.
    start_supervised!({Finch, name: __MODULE__.BigBodyFinch})
    wl = unique("wl")
    big = :binary.copy("a", 8_388_609)

    {:ok, resp} =
      Finch.build(:post, "http://127.0.0.1:8080/v1/workloads/#{wl}/tasks", auth("good"), big)
      |> Finch.request(__MODULE__.BigBodyFinch)

    assert resp.status == 413
  end

  # -- reads -----------------------------------------------------------------

  test "GET unknown task is 404" do
    resp = req(:get, "/v1/tasks/#{unique("nope")}", auth("good"))
    assert resp.status == 404
  end

  test "GET result is 404 before any result exists" do
    wl = unique("wl")
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", auth("good"), "x").body)["task_id"]

    resp = req(:get, "/v1/tasks/#{task_id}/result", auth("good"))
    assert resp.status == 404
  end

  # -- sync ------------------------------------------------------------------

  test "sync submit returns the stored guest result when the task is already terminal" do
    wl = unique("wl")
    key = unique("idem")
    headers = auth("good") ++ [{"idempotency-key", key}]

    # Create the task, then drive it to success directly (no dispatcher in R0).
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", headers, "x").body)["task_id"]
    {:ok, _} = TaskStore.assign(task_id)
    {:ok, _} = TaskStore.start(task_id)

    {:ok, _} =
      TaskStore.succeed(task_id, %{
        status_code: 201,
        body: "FINDINGS",
        size_bytes: 8,
        truncated: false
      })

    # A sync submit with the same idempotency key resolves to the existing,
    # now-terminal task; the already-terminal re-check returns its result without
    # a real park.
    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", headers, "x")
    assert resp.status == 201
    assert resp.body == "FINDINGS"
  end

  # -- guest response headers ------------------------------------------------

  defp header(resp, name) do
    Enum.find_value(resp.headers, fn {k, v} -> if String.downcase(k) == name, do: v end)
  end

  test "sync submit replays the guest content-type and strips framing headers" do
    wl = unique("wl")
    key = unique("idem")
    headers = auth("good") ++ [{"idempotency-key", key}]

    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", headers, "x").body)["task_id"]
    {:ok, _} = TaskStore.assign(task_id)
    {:ok, _} = TaskStore.start(task_id)

    {:ok, _} =
      TaskStore.succeed(task_id, %{
        status_code: 200,
        body: "PNGDATA",
        size_bytes: 7,
        truncated: false,
        headers: %{
          "content-type" => "image/png",
          "x-custom" => "yes",
          # framing / hop-by-hop headers the server must own: stripped.
          "content-length" => "999",
          "transfer-encoding" => "chunked",
          "connection" => "keep-alive"
        }
      })

    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", headers, "x")
    assert resp.status == 200
    assert resp.body == "PNGDATA"

    # The guest content-type wins over the octet-stream default.
    assert header(resp, "content-type") == "image/png"
    assert header(resp, "x-custom") == "yes"
    # x-ember-truncated is still set by the control plane.
    assert header(resp, "x-ember-truncated") == "false"
    # Framing headers the guest tried to set were stripped (the server owns them);
    # content-length reflects the real body, never the guest's bogus "999".
    assert header(resp, "transfer-encoding") == nil
    assert header(resp, "content-length") == "7"
  end

  test "sync submit falls back to octet-stream when the guest set no headers" do
    wl = unique("wl")
    key = unique("idem")
    headers = auth("good") ++ [{"idempotency-key", key}]

    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", headers, "x").body)["task_id"]
    {:ok, _} = TaskStore.assign(task_id)
    {:ok, _} = TaskStore.start(task_id)

    {:ok, _} =
      TaskStore.succeed(task_id, %{
        status_code: 200,
        body: "BYTES",
        size_bytes: 5,
        truncated: false
      })

    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", headers, "x")
    assert resp.status == 200
    # Plug's put_resp_content_type appends "; charset=utf-8" to the fallback, the
    # same as the pre-change behavior, so match the prefix rather than the exact value.
    assert header(resp, "content-type") |> String.starts_with?("application/octet-stream")
    assert header(resp, "x-ember-truncated") == "false"
  end

  test "sync submit is 429 when the principal's park cap is exhausted" do
    Application.put_env(:embervm, :sync_park_cap, 0)
    wl = unique("wl")

    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", auth("good"), "x")
    assert resp.status == 429
    assert json(resp.body)["retryable"] == true
  end

  test "sync submit times out to 202 while work is still in flight" do
    Application.put_env(:embervm, :sync_timeout_ms, 60)
    wl = unique("wl")

    resp = req(:post, "/v1/workloads/#{wl}/tasks?wait=true", auth("good"), "x")
    assert resp.status == 202
    assert json(resp.body)["state"] == "queued"
  end

  # -- DLQ + redrive ---------------------------------------------------------

  test "dead-letter listing and redrive round-trip" do
    wl = unique("wl")

    {:ok, :created, task_id} =
      TaskStore.submit(%{tenant: "homelab", principal: @allowed, workload: wl})

    {:ok, _} = TaskStore.assign(task_id)
    # guest4xx is always permanent, so this dead-letters in one step.
    {:ok, dl} = TaskStore.fail(task_id, :guest4xx)
    assert dl.state == :dead_lettered

    listed = req(:get, "/v1/workloads/#{wl}/dead-letters", auth("good"))
    assert listed.status == 200
    body = json(listed.body)
    assert body["total"] == 1
    assert [item] = body["items"]
    assert item["task_id"] == task_id
    assert item["state"] == "dead_lettered"

    redriven = req(:post, "/v1/tasks/#{task_id}/redrive", auth("good"))
    assert redriven.status == 200
    assert json(redriven.body)["state"] == "queued"

    got = req(:get, "/v1/tasks/#{task_id}", auth("good"))
    assert json(got.body)["state"] == "queued"
    # Attempt counter was reset to a fresh budget.
    assert json(got.body)["attempt"] == 1
  end

  test "redrive of a non-dead-lettered task is 409" do
    wl = unique("wl")
    {:ok, :created, task_id} = TaskStore.submit(%{tenant: "homelab", principal: @allowed, workload: wl})

    resp = req(:post, "/v1/tasks/#{task_id}/redrive", auth("good"))
    assert resp.status == 409
  end

  # -- metering + quotas (Task 12) -------------------------------------------

  # Drive a submitted task terminal with usage directly through the store (no
  # dispatcher in R0), so GET /v1/usage has a row to serve.
  defp bill(task_id, cpu_ms) do
    {:ok, _} = TaskStore.assign(task_id)
    {:ok, _} = TaskStore.start(task_id)

    {:ok, _} =
      TaskStore.succeed(
        TaskStore,
        task_id,
        %{status_code: 200, body: "", size_bytes: 0, truncated: false, expires_at: nil},
        %{cpu_ms: cpu_ms, peak_rss_mib: 1024, wall_ms: 1000}
      )
  end

  test "GET /v1/usage returns the caller's own billed usage" do
    wl = unique("wl")
    task_id = json(req(:post, "/v1/workloads/#{wl}/tasks", auth("good"), "x").body)["task_id"]
    bill(task_id, 3000)

    resp = req(:get, "/v1/usage", auth("good"))
    assert resp.status == 200

    mine = Enum.find(json(resp.body)["items"], &(&1["principal"] == @allowed))
    assert mine
    # 3000ms = 3.0 vCPU-s; accumulates across this file's tests, so >=.
    assert mine["vcpu_seconds"] >= 3.0
  end

  test "GET /v1/usage is self-scoped: a non-admin cannot read another principal" do
    resp = req(:get, "/v1/usage?principal=#{URI.encode_www_form(@allowed)}", auth("good2"))
    assert resp.status == 200
    # good2 (principal-2, not an admin) is forced to its own scope, never @allowed.
    refute Enum.any?(json(resp.body)["items"], &(&1["principal"] == @allowed))
  end

  test "a usage admin can read another principal via ?principal=" do
    Application.put_env(:embervm, :usage_admins, ["principal-2"])

    resp = req(:get, "/v1/usage?principal=#{URI.encode_www_form(@allowed)}", auth("good2"))
    assert resp.status == 200
    # Every returned row is the requested principal (the admin filter took effect).
    assert Enum.all?(json(resp.body)["items"], &(&1["principal"] == @allowed))
  end

  test "submit is 429 when the principal's daily vCPU-second quota is exhausted" do
    Application.put_env(:embervm, :quota, %{budgets: %{@allowed => 0.0}, default: nil})

    resp = req(:post, "/v1/workloads/#{unique("wl")}/tasks", auth("good"), "x")
    assert resp.status == 429
    body = json(resp.body)
    assert body["error"] =~ "quota"
    assert body["retryable"] == true
  end

  # -- session routes (R2) ---------------------------------------------------

  test "POST /v1/workloads/:name/sessions creates a session and returns the token once (management auth)" do
    with_session_fakes()

    resp = req(:post, "/v1/workloads/wl-ok/sessions", auth("good"))
    assert resp.status == 201
    body = json(resp.body)
    assert body["session_id"] == "s-live"
    assert body["session_token"] == "sess-token-live"
    assert body["state"] == "running"
  end

  test "session create requires management auth (401 without a token)" do
    with_session_fakes()
    assert req(:post, "/v1/workloads/wl-ok/sessions").status == 401
  end

  test "session create denials map to distinguishable statuses" do
    with_session_fakes()

    cap = req(:post, "/v1/workloads/wl-cap/sessions", auth("good"))
    assert cap.status == 429
    assert json(cap.body)["reason"] == "session_cap"

    task = req(:post, "/v1/workloads/wl-task/sessions", auth("good"))
    assert task.status == 403
    assert json(task.body)["reason"] == "not_session_class"

    unknown = req(:post, "/v1/workloads/wl-nope/sessions", auth("good"))
    assert unknown.status == 404
  end

  test "invoke is gated on the SESSION token: a management token alone is rejected 403" do
    with_session_fakes()

    # A valid MANAGEMENT token ("good") is NOT this session's token: 403.
    mgmt = req(:post, "/v1/sessions/s-live/invoke", auth("good"), "payload")
    assert mgmt.status == 403

    # No token at all: 401.
    assert req(:post, "/v1/sessions/s-live/invoke", [], "x").status == 401

    # The session's own token: the invoke proxies and returns the guest response.
    ok = req(:post, "/v1/sessions/s-live/invoke", auth("sess-token-live"), "payload")
    assert ok.status == 200
    assert ok.body == "echoed"
    assert Enum.any?(ok.headers, fn {k, v} -> k == "content-type" and v =~ "text/plain" end)
  end

  test "invoke on a terminal session (valid token) is 410 with the reason" do
    with_session_fakes()

    resp = req(:post, "/v1/sessions/s-term/invoke", auth("sess-token-term"), "x")
    assert resp.status == 410
    assert json(resp.body)["reason"] == "destroyed"
  end

  test "invoke queue-full maps to 429" do
    with_session_fakes()

    # s-queue authorizes with its own token and the manager fake returns :queue_full.
    resp = req(:post, "/v1/sessions/s-queue/invoke", auth("sess-token-queue"), "x")
    assert resp.status == 429
  end

  test "GET /v1/sessions/:id accepts the session token OR a management token" do
    with_session_fakes()

    # Session token.
    a = req(:get, "/v1/sessions/s-live", auth("sess-token-live"))
    assert a.status == 200
    assert json(a.body)["session_id"] == "s-live"

    # Management token (TokenReview via FakeAuth "good").
    b = req(:get, "/v1/sessions/s-live", auth("good"))
    assert b.status == 200

    # A bad token is 403.
    assert req(:get, "/v1/sessions/s-live", auth("nope")).status == 403

    # No token is 401.
    assert req(:get, "/v1/sessions/s-live").status == 401
  end

  test "DELETE /v1/sessions/:id destroys (management auth)" do
    with_session_fakes()

    assert req(:delete, "/v1/sessions/s-live", auth("good")).status == 200
    assert req(:delete, "/v1/sessions/s-nope", auth("good")).status == 404
    # Management auth required.
    assert req(:delete, "/v1/sessions/s-live").status == 401
  end

  test "GET /v1/workloads/:name/sessions lists (management auth)" do
    with_session_fakes()

    resp = req(:get, "/v1/workloads/wl-ok/sessions", auth("good"))
    assert resp.status == 200
    body = json(resp.body)
    assert body["workload"] == "wl-ok"
    assert body["items"] == []
  end
end
