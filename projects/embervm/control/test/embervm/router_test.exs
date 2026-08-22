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
  import ExUnit.CaptureLog

  alias Embervm.{NodeRegistry, TaskStore}

  @allowed "system:serviceaccount:embervm:embervm"

  defmodule FakeAuth do
    @allowed "system:serviceaccount:embervm:embervm"
    def authenticate("good"), do: {:ok, @allowed}
    def authenticate("good2"), do: {:ok, "principal-2"}
    def authenticate("forbidden"), do: {:error, :forbidden}
    def authenticate(_), do: {:error, :unauthenticated}

    def authenticate_identity("good") do
      {:ok,
       %Embervm.Auth.Identity{
         username: @allowed,
         pod_uid: "router-test-sa",
         pod_name: "embervm-brick-good",
         node_name: "node-4"
       }}
    end

    def authenticate_identity("good2") do
      {:ok,
       %Embervm.Auth.Identity{
         username: "principal-2",
         pod_uid: "router-test-other",
         pod_name: "embervm-brick-other",
         node_name: "node-4"
       }}
    end

    def authenticate_identity("legacy") do
      {:ok,
       %Embervm.Auth.Identity{
         username: @allowed,
         pod_uid: nil,
         pod_name: nil,
         node_name: nil
       }}
    end

    def authenticate_identity(_), do: {:error, :unauthenticated}
  end

  # Fakes for the R2 session routes: the router resolves the session manager/store
  # from app-env (the :session_manager / :session_store_mod keys), so a request test
  # can drive the HTTP surface, and especially the SESSION-TOKEN auth boundary,
  # without a live daemon or the supervised SessionManager.
  defmodule FakeSessionManager do
    def create(_srv, "wl-ok", _principal, _restore_lineage),
      do:
        {:ok,
         %{
           session_id: "s-live",
           lineage_id: "s-live",
           token: "sess-token-live",
           expires_at: 9_000_000,
           base_digest: "sha256:x",
           state: :running,
           restored: false
         }}

    def create(_srv, "wl-cap", _principal, _restore_lineage), do: {:error, {:denied, :session_cap}}
    def create(_srv, "wl-vmcap", _principal, _restore_lineage), do: {:error, {:denied, :workload_cap}}
    def create(_srv, "wl-task", _principal, _restore_lineage), do: {:error, {:denied, :not_session_class}}

    # #4306 slice 3: restore_lineage-carrying creates. wl-restore-ok only
    # returns restored: true when it actually RECEIVED restore_lineage (a
    # binary), so the test proves the router threads the body field through
    # rather than the fake just always answering true. lineage_id echoes the
    # RECEIVED restore_lineage (item B): the response must expose the same
    # inherited lineage handle the caller sent, not the fresh session_id.
    def create(_srv, "wl-restore-ok", _principal, restore_lineage) when is_binary(restore_lineage) and restore_lineage != "",
      do:
        {:ok,
         %{
           session_id: "s-restored",
           lineage_id: restore_lineage,
           token: "sess-token-restored",
           expires_at: 9_000_000,
           base_digest: "sha256:x",
           state: :running,
           restored: true
         }}

    def create(_srv, "wl-unknown-lineage", _principal, _restore_lineage), do: {:error, {:denied, :unknown_lineage}}
    def create(_srv, "wl-lineage-workload-mismatch", _principal, _restore_lineage), do: {:error, {:denied, :lineage_workload_mismatch}}
    def create(_srv, "wl-lineage-principal-mismatch", _principal, _restore_lineage), do: {:error, {:denied, :lineage_principal_mismatch}}
    def create(_srv, "wl-lineage-live-heir", _principal, _restore_lineage), do: {:error, {:denied, :lineage_live_heir}}

    # #4306/#4313 review fix 2: TOCTOU guard denial.
    def create(_srv, "wl-lineage-restore-in-flight", _principal, _restore_lineage),
      do: {:error, {:denied, :lineage_restore_in_flight}}

    def create(_srv, _wl, _principal, _restore_lineage), do: {:error, {:denied, :unknown_workload}}

    def invoke(_srv, "s-live", _req), do: {:ok, %{status_code: 200, headers: %{"content-type" => "text/plain"}, body: "echoed"}}
    def invoke(_srv, "s-queue", _req), do: {:error, :queue_full}
    def invoke(_srv, "s-pressure", _req), do: {:error, {:relight_failed, {:prime_failed, %GRPC.RPCError{status: 8, message: "pressure:mem"}}}}
    def invoke(_srv, "s-snapshot", _req), do: {:error, {:relight_failed, {:prime_failed, %GRPC.RPCError{status: 9, message: "snapshot lost"}}}}
    def invoke(_srv, _id, _req), do: {:error, :not_found}

    def destroy(_srv, "s-live"), do: {:ok, :destroyed}
    def destroy(_srv, "s-destroying"), do: {:ok, :destroying}
    def destroy(_srv, "s-error"), do: {:error, :backend_down}
    def destroy(_srv, _id), do: {:error, :not_found}
  end

  defmodule FakeSessionStore do
    # s-live's token is "sess-token-live"; any other token is unauthorized.
    def verify_token(_srv, "s-live", "sess-token-live"), do: {:ok, %{session_id: "s-live"}}
    def verify_token(_srv, "s-live", _), do: {:error, :unauthorized}
    def verify_token(_srv, "s-queue", "sess-token-queue"), do: {:ok, %{session_id: "s-queue"}}
    def verify_token(_srv, "s-pressure", "sess-token-pressure"), do: {:ok, %{session_id: "s-pressure"}}
    def verify_token(_srv, "s-snapshot", "sess-token-snapshot"), do: {:ok, %{session_id: "s-snapshot"}}
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

  # A tiny upstream "guest" Plug the activator proxies to: echoes the request path
  # and body, sets a custom content-type (to prove header carry) and a hop-by-hop
  # header (to prove it is stripped), streaming the body in two chunks (to prove the
  # response is not buffered whole).
  defmodule GuestPlug do
    import Plug.Conn

    def init(opts), do: opts

    def call(conn, _opts) do
      {:ok, body, conn} = read_body(conn)

      conn
      |> put_resp_content_type("text/plain")
      |> put_resp_header("x-guest-echo", conn.request_path)
      # A hop-by-hop header the proxy MUST strip.
      |> put_resp_header("connection", "keep-alive")
      |> send_chunked(200)
      |> then(fn c ->
        {:ok, c} = chunk(c, "hello:")
        {:ok, c} = chunk(c, body)
        c
      end)
    end
  end

  # The router resolves the serving manager from :serving_manager_mod; this fake
  # returns a live endpoint pointing at the GuestPlug server a test starts, so the
  # activator route + Embervm.ServingProxy stream against a REAL upstream.
  defmodule FakeServingManager do
    def miss(_srv, "wl-live", _req, _principal) do
      {:ok, %{ip: "127.0.0.1", port: Application.get_env(:embervm, :test_guest_port)}}
    end

    def miss(_srv, "wl-429", _req, _principal), do: {:error, {:wake_rate, "rate"}}
    def miss(_srv, "wl-503", _req, _principal), do: {:error, {:wake_failed, :readiness_timeout}}
    def miss(_srv, _wl, _req, _principal), do: {:error, {:unknown_workload}}
  end

  # Fakes for the R4 stateful GET route: the router resolves the stateful store from
  # :stateful_store_mod and the catalog from :workload_catalog_mod/:workload_catalog_table,
  # so a request test can drive GET /v1/stateful/:name without the live StatefulStore
  # or WorkloadWatcher. wl-live has a live serving instance; wl-banked has only a banked
  # instance (pair populated); wl-unknown is not a stateful workload (404).
  defmodule FakeStatefulStore do
    def list(_srv, "wl-live") do
      [
        %{
          instance_id: "sf-live",
          workload: "wl-live",
          state: :serving,
          healthy: true,
          node_id: "node-4",
          ip: "10.99.0.7",
          port: 6000,
          generation: 3,
          snapshot_ref: nil,
          snapshot_generation: nil,
          created_at: 1,
          last_active_at: 5,
          updated_at: 6,
          terminal_reason: nil
        }
      ]
    end

    def list(_srv, "wl-banked") do
      [
        %{
          instance_id: "sf-banked",
          workload: "wl-banked",
          state: :banked,
          healthy: false,
          node_id: "node-4",
          ip: nil,
          port: nil,
          generation: 2,
          snapshot_ref: "stateful/wl-banked/s",
          snapshot_generation: 2,
          created_at: 1,
          last_active_at: nil,
          updated_at: 4,
          terminal_reason: nil
        }
      ]
    end

    def list(_srv, _wl), do: []

    def get_volume(_srv, "wl-live"), do: %{workload: "wl-live", generation: 3, allocated_bytes: 111}
    def get_volume(_srv, "wl-banked"), do: %{workload: "wl-banked", generation: 2, allocated_bytes: 222}
    def get_volume(_srv, _wl), do: nil

    def pair_valid?(_srv, "wl-banked"), do: true
    def pair_valid?(_srv, _wl), do: false

    def published_endpoint(_srv, "wl-live"), do: %{ip: "10.99.0.7", port: 6000}
    def published_endpoint(_srv, _wl), do: nil
  end

  defmodule FakeCatalog do
    def fetch(_table, "wl-live"), do: {:ok, %{class: "stateful", stateful: %{listen_port: 9100}}}
    def fetch(_table, "wl-banked"), do: {:ok, %{class: "stateful", stateful: %{listen_port: 9101}}}
    def fetch(_table, "wl-serving"), do: {:ok, %{class: "serving", serving: %{host: "h"}}}
    def fetch(_table, _name), do: :error
  end

  # The router resolves the stateful manager from :stateful_manager_mod for the
  # DELETE /v1/stateful/:name/instance + /volume handlers, mirroring
  # FakeServingManager for the forced-roll handler. wl-live destroys/evicts
  # cleanly; wl-blocked's delete_volume refuses with :instance_exists (409);
  # wl-boom's delete_volume raises a generic store error (500).
  defmodule FakeStatefulManager do
    def destroy_instance(_srv, "wl-live"), do: %{destroyed: 1, evicted: 0}
    def destroy_instance(_srv, "wl-empty"), do: %{destroyed: 0, evicted: 0}
    def destroy_instance(_srv, _wl), do: %{destroyed: 0, evicted: 1}

    def delete_volume(_srv, "wl-blocked"), do: {:error, :instance_exists}
    def delete_volume(_srv, "wl-incomplete"), do: {:error, {:delete_incomplete, ["node-2"]}}
    def delete_volume(_srv, "wl-boom"), do: {:error, {:store, :disk_full}}
    def delete_volume(_srv, _wl), do: {:ok, %{deleted: true}}
  end

  setup do
    Application.put_env(:embervm, :authenticator, FakeAuth)

    on_exit(fn ->
      # Only the two registers that return 200 actually create an instance; the
      # 401/403 cases are rejected before the registry sees them. Unregister is
      # synchronous, so the streamer and its pending reconnect are gone before
      # the next test file opens its trace store. Without this the retry loop
      # outlives the test (async: false means the same VM) and writes health
      # records into someone else's trace.
      NodeRegistry.unregister("node-4", "router-test-sa")
      NodeRegistry.unregister("node-4", "router-test-node-token")
      Application.delete_env(:embervm, :authenticator)
      Application.delete_env(:embervm, :sync_park_cap)
      Application.delete_env(:embervm, :sync_timeout_ms)
      Application.delete_env(:embervm, :quota)
      Application.delete_env(:embervm, :usage_admins)
      Application.delete_env(:embervm, :session_manager)
      Application.delete_env(:embervm, :session_store_mod)
      Application.delete_env(:embervm, :serving_manager_mod)
      Application.delete_env(:embervm, :test_guest_port)
      Application.delete_env(:embervm, :stateful_store_mod)
      Application.delete_env(:embervm, :workload_catalog_mod)
      Application.delete_env(:embervm, :stateful_manager_mod)
      Application.delete_env(:embervm, :noded_service_account)
    end)

    :ok
  end

  defp with_stateful_fakes do
    Application.put_env(:embervm, :stateful_store_mod, FakeStatefulStore)
    Application.put_env(:embervm, :workload_catalog_mod, FakeCatalog)
  end

  defp with_stateful_manager_fake do
    Application.put_env(:embervm, :stateful_manager_mod, FakeStatefulManager)
  end

  defp with_serving_fake do
    Application.put_env(:embervm, :serving_manager_mod, FakeServingManager)
  end

  # Start the upstream guest server on a fixed high port (safe: router_test is
  # async: false, so no parallel test binds it) and record it for the fake.
  @guest_port 8099
  defp start_guest do
    start_supervised!({Bandit, plug: GuestPlug, scheme: :http, port: @guest_port})
    Application.put_env(:embervm, :test_guest_port, @guest_port)
    @guest_port
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

  # -- node dial-home registration (R0 PR-2) ---------------------------------

  defp reg_body(overrides \\ %{}) do
    Map.merge(
      # The address must be a port that is REFUSED identically on every host, not
      # merely one that happens to be unreachable. This used to advertise
      # 10.0.0.9:9090, which the registry then dialled for real: on a runner with
      # a route the dial hung and the node aged unknown -> down, while on one
      # without a route it failed instantly with :enetunreach and went straight
      # to down, tripping the health_monotonic spec-trace invariant in whichever
      # test happened to own the trace store by then (#4828). 127.0.0.1:1 is
      # refused the same way everywhere.
      #
      # pod_uid stays unique per registration. Collapsing it to a constant would
      # turn the second successful register in this file into a re-registration
      # of the same (node, pod_uid), which is a distinct code path and the one
      # #4707 is about. Teardown names the uids explicitly instead.
      %{"node" => "node-4", "pod_uid" => unique("uid"), "address" => "127.0.0.1:1", "boot_id" => "boot-1"},
      overrides
    )
    |> :json.encode()
    |> :erlang.iolist_to_binary()
  end

  test "POST /v1/nodes/register with the noded SA token is 200" do
    Application.put_env(:embervm, :noded_service_account, @allowed)
    resp = req(:post, "/v1/nodes/register", auth("good"), reg_body(%{"pod_uid" => "router-test-sa"}))
    assert resp.status == 200
    assert json(resp.body)["registered"] == true
  end

  test "POST /v1/nodes/register without a token is 401" do
    Application.put_env(:embervm, :noded_service_account, @allowed)
    resp = req(:post, "/v1/nodes/register", [], reg_body())
    assert resp.status == 401
  end

  test "POST /v1/nodes/register with a valid token that is NOT the noded SA is 403" do
    # good2 authenticates as "principal-2", not the configured noded SA.
    Application.put_env(:embervm, :noded_service_account, @allowed)
    resp = req(:post, "/v1/nodes/register", auth("good2"), reg_body())
    assert resp.status == 403
  end

  test "POST /v1/nodes/register rejects a pod_uid that differs from the token claim" do
    Application.put_env(:embervm, :noded_service_account, @allowed)
    parent = self()

    log =
      capture_log(fn ->
        resp =
          req(
            :post,
            "/v1/nodes/register",
            auth("good"),
            reg_body(%{"pod_uid" => "claimed-other-pod"})
          )

        send(parent, {:registration_response, resp})
      end)

    assert_receive {:registration_response, resp}
    assert resp.status == 403
    assert json(resp.body) == %{"error" => "registration identity does not match token", "retryable" => false}
    assert length(Regex.scan(~r/registration_identity_mismatch/, log)) == 1
    assert log =~ "claimed_node=\"node-4\""
    assert log =~ "claimed_pod_uid=\"claimed-other-pod\""
    assert log =~ "token_pod_uid=\"router-test-sa\""
    assert log =~ "token_pod_name=\"embervm-brick-good\""
  end

  test "POST /v1/nodes/register rejects a node that differs from the token claim" do
    Application.put_env(:embervm, :noded_service_account, @allowed)

    resp =
      req(
        :post,
        "/v1/nodes/register",
        auth("good"),
        reg_body(%{"node" => "node-5", "pod_uid" => "router-test-sa"})
      )

    assert resp.status == 403
    assert json(resp.body)["error"] == "registration identity does not match token"
  end

  test "POST /v1/nodes/register rejects a token without a bound pod_uid" do
    Application.put_env(:embervm, :noded_service_account, @allowed)
    resp = req(:post, "/v1/nodes/register", auth("legacy"), reg_body())
    assert resp.status == 403
    assert json(resp.body)["error"] == "registration identity does not match token"
  end

  test "POST /v1/nodes/register rejects an empty body pod_uid" do
    Application.put_env(:embervm, :noded_service_account, @allowed)
    resp = req(:post, "/v1/nodes/register", auth("good"), reg_body(%{"pod_uid" => ""}))
    assert resp.status == 403
    assert json(resp.body)["error"] == "registration identity does not match token"
  end

  test "POST /v1/nodes/register fails closed when the noded SA is not configured" do
    Application.put_env(:embervm, :noded_service_account, "")
    resp = req(:post, "/v1/nodes/register", auth("good"), reg_body(%{"pod_uid" => "router-test-sa"}))
    assert resp.status == 403
    assert json(resp.body) == %{"error" => "noded service account not configured", "retryable" => false}
  end

  test "POST /v1/nodes/register accepts a forbidden-but-valid token as the noded SA" do
    # A node token that TokenReviews to the noded SA but is NOT on the task-submit
    # allow-list carries its reviewed Identity in the forbidden result; node auth
    # accepts it.
    Application.put_env(:embervm, :noded_service_account, "system:serviceaccount:embervm:node")

    defmodule NodeAuth do
      def authenticate("node-token"), do: {:error, {:forbidden, "system:serviceaccount:embervm:node"}}
      def authenticate(_), do: {:error, :unauthenticated}

      def authenticate_identity("node-token") do
        {:error,
         {:forbidden,
          %Embervm.Auth.Identity{
            username: "system:serviceaccount:embervm:node",
            pod_uid: "router-test-node-token",
            pod_name: "embervm-brick-node-token",
            node_name: "node-4"
          }}}
      end

      def authenticate_identity(_), do: {:error, :unauthenticated}
    end

    Application.put_env(:embervm, :authenticator, NodeAuth)
    resp = req(:post, "/v1/nodes/register", auth("node-token"), reg_body(%{"pod_uid" => "router-test-node-token"}))
    assert resp.status == 200
  end

  test "POST /v1/nodes/register with a malformed body is still 200 (advertisement, benign)" do
    Application.put_env(:embervm, :noded_service_account, @allowed)
    resp = req(:post, "/v1/nodes/register", auth("good"), "not json")
    assert resp.status == 200
  end

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
    # #4306 slice 3 review fix (item B): a normal create's lineage_id equals
    # its session_id, and the RESPONSE (not just the internal created map)
    # must expose it, since Slice 4 restores off this field.
    assert body["lineage_id"] == "s-live"
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

    vmcap = req(:post, "/v1/workloads/wl-vmcap/sessions", auth("good"))
    assert vmcap.status == 429
    assert json(vmcap.body)["reason"] == "workload_cap"

    task = req(:post, "/v1/workloads/wl-task/sessions", auth("good"))
    assert task.status == 403
    assert json(task.body)["reason"] == "not_session_class"

    unknown = req(:post, "/v1/workloads/wl-nope/sessions", auth("good"))
    assert unknown.status == 404
  end

  # -- restore_lineage (#4306 slice 3) ---------------------------------------

  test "POST .../sessions with a restore_lineage body threads it through and reports restored" do
    with_session_fakes()

    resp =
      req(:post, "/v1/workloads/wl-restore-ok/sessions", auth("good"), ~s({"restore_lineage": "lineage-abc"}))

    assert resp.status == 201
    body = json(resp.body)
    assert body["session_id"] == "s-restored"
    assert body["restored"] == true
    # #4306 slice 3 review fix (item B): the response's lineage_id is the
    # INHERITED lineage handle, not the fresh session_id -- the divergence a
    # Slice 4 caller chaining generations must see to restore again.
    assert body["lineage_id"] == "lineage-abc"
    assert body["session_id"] != body["lineage_id"]
  end

  test "a normal create (no restore_lineage body) reports restored: false" do
    with_session_fakes()

    resp = req(:post, "/v1/workloads/wl-ok/sessions", auth("good"))
    assert resp.status == 201
    assert json(resp.body)["restored"] == false
  end

  test "an empty or malformed restore_lineage body is treated as a normal create, not an error" do
    with_session_fakes()

    empty_body = req(:post, "/v1/workloads/wl-ok/sessions", auth("good"), ~s({"restore_lineage": ""}))
    assert empty_body.status == 201
    assert json(empty_body.body)["restored"] == false

    garbage = req(:post, "/v1/workloads/wl-ok/sessions", auth("good"), "not json")
    assert garbage.status == 201
    assert json(garbage.body)["restored"] == false
  end

  test "restore_lineage validation denials map to distinguishable statuses" do
    with_session_fakes()
    body = ~s({"restore_lineage": "lineage-x"})

    unknown = req(:post, "/v1/workloads/wl-unknown-lineage/sessions", auth("good"), body)
    assert unknown.status == 404
    assert json(unknown.body)["reason"] == "unknown_lineage"

    wl_mismatch = req(:post, "/v1/workloads/wl-lineage-workload-mismatch/sessions", auth("good"), body)
    assert wl_mismatch.status == 403
    assert json(wl_mismatch.body)["reason"] == "lineage_workload_mismatch"

    principal_mismatch = req(:post, "/v1/workloads/wl-lineage-principal-mismatch/sessions", auth("good"), body)
    assert principal_mismatch.status == 403
    assert json(principal_mismatch.body)["reason"] == "lineage_principal_mismatch"

    live_heir = req(:post, "/v1/workloads/wl-lineage-live-heir/sessions", auth("good"), body)
    assert live_heir.status == 409
    assert json(live_heir.body)["reason"] == "lineage_live_heir"
    assert json(live_heir.body)["retryable"] == false
  end

  test "restore_lineage in-flight guard denial is 409 and retryable (#4306/#4313 review fix 2)" do
    with_session_fakes()

    resp =
      req(:post, "/v1/workloads/wl-lineage-restore-in-flight/sessions", auth("good"), ~s({"restore_lineage": "lineage-x"}))

    assert resp.status == 409
    body = json(resp.body)
    assert body["reason"] == "lineage_restore_in_flight"
    # Unlike lineage_live_heir (a committed heir, not worth retrying), an
    # in-flight restore is transient: the client may simply retry.
    assert body["retryable"] == true
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

  test "invoke relight RESOURCE_EXHAUSTED is retryable" do
    with_session_fakes()

    resp = req(:post, "/v1/sessions/s-pressure/invoke", auth("sess-token-pressure"), "x")
    assert resp.status == 502
    assert json(resp.body)["retryable"] == true
  end

  test "invoke relight non-RESOURCE_EXHAUSTED failure is not retryable" do
    with_session_fakes()

    resp = req(:post, "/v1/sessions/s-snapshot/invoke", auth("sess-token-snapshot"), "x")
    assert resp.status == 502
    assert json(resp.body)["retryable"] == false
  end

  test "classify_error_as_retryable handles nested errors and other statuses" do
    assert Embervm.Router.classify_error_as_retryable({:relight_failed, {:prime_failed, %GRPC.RPCError{status: 8}}})
    refute Embervm.Router.classify_error_as_retryable({:relight_failed, {:prime_failed, %GRPC.RPCError{status: 9}}})
    refute Embervm.Router.classify_error_as_retryable({:relight_failed, {:prime_failed, %GRPC.RPCError{status: 2}}})
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

    destroyed = req(:delete, "/v1/sessions/s-live", auth("good"))
    assert destroyed.status == 200
    assert json(destroyed.body)["state"] == "destroyed"

    destroying = req(:delete, "/v1/sessions/s-destroying", auth("good"))
    assert destroying.status == 202
    assert json(destroying.body)["state"] == "destroying"

    assert req(:delete, "/v1/sessions/s-nope", auth("good")).status == 404
    assert req(:delete, "/v1/sessions/s-error", auth("good")).status == 500
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

  # -- activator (R3, Task 8) ------------------------------------------------

  test "the activator route proxies a miss to the woken guest and streams the response" do
    with_serving_fake()
    start_guest()

    # A request Envoy routed to the activator: NO bearer token (end-user traffic),
    # the x-ember-workload header injected by the serving route, an arbitrary path.
    resp = req(:post, "/og-image?ref=abc", [{"x-ember-workload", "wl-live"}], "world")

    assert resp.status == 200
    # The guest streamed "hello:" <> body; header carry preserved the guest's
    # content-type and the echo header, and stripped the hop-by-hop connection header.
    assert resp.body == "hello:world"
    assert {"content-type", "text/plain" <> _} = List.keyfind(resp.headers, "content-type", 0)
    assert {"x-guest-echo", "/og-image"} = List.keyfind(resp.headers, "x-guest-echo", 0)
    refute List.keyfind(resp.headers, "connection", 0) == {"connection", "keep-alive"}
  end

  test "the activator route maps wake denials to statuses" do
    with_serving_fake()

    assert req(:get, "/x", [{"x-ember-workload", "wl-429"}]).status == 429
    assert req(:get, "/x", [{"x-ember-workload", "wl-503"}]).status == 503
    assert req(:get, "/x", [{"x-ember-workload", "wl-unknown"}]).status == 404
  end

  test "activator error responses are generic and do NOT leak internals" do
    # The activator miss path is PUBLIC, so a wake-failure body must not expose the gRPC
    # reason (tap IP/DNAT port/shim path), the workload name, or any internal detail; the
    # diagnostic detail stays in the server logs (D-R3.11.x error-leak fix). wl-503's fake
    # miss returns {:wake_failed, :readiness_timeout}.
    with_serving_fake()
    resp = req(:get, "/x", [{"x-ember-workload", "wl-503"}])

    assert resp.status == 503
    assert resp.body =~ "service temporarily unavailable"
    refute resp.body =~ "readiness_timeout"
    refute resp.body =~ "wl-503"
    refute resp.body =~ "reason"
  end

  test "an unmatched path WITHOUT the activator header is a plain 404, not a miss" do
    with_serving_fake()
    # No x-ember-workload header: the catch-all 404s rather than treating it as a miss.
    assert req(:get, "/totally-unknown-path").status == 404
  end

  # -- GET /v1/stateful/:name (R4) -------------------------------------------

  test "GET /v1/stateful/:name returns 200 with the live instance + published endpoint" do
    with_stateful_fakes()

    resp = req(:get, "/v1/stateful/wl-live", auth("good"))
    assert resp.status == 200
    body = json(resp.body)

    assert body["workload"] == "wl-live"
    assert body["state"] == "serving"
    assert body["generation"] == 3
    # No banked bundle: bundle_generation is null.
    assert body["bundle_generation"] == nil
    # A live serving instance's volume matches (fake pair_valid? false here since only
    # wl-banked is paired), volume_bytes surfaced from the volume row.
    assert body["volume_bytes"] == 111
    assert body["published_endpoint"] == %{"ip" => "10.99.0.7", "port" => 6000}

    inst = body["instance"]
    assert inst["instance_id"] == "sf-live"
    assert inst["state"] == "serving"
    assert inst["healthy"] == true
    assert inst["ip"] == "10.99.0.7"
    assert inst["port"] == 6000
  end

  test "GET /v1/stateful/:name returns 200 for a banked-only workload with the pair populated" do
    with_stateful_fakes()

    resp = req(:get, "/v1/stateful/wl-banked", auth("good"))
    assert resp.status == 200
    body = json(resp.body)

    assert body["workload"] == "wl-banked"
    assert body["state"] == "banked"
    # The banked bundle's stamped generation is the pair key.
    assert body["bundle_generation"] == 2
    assert body["pair_valid"] == true
    assert body["volume_bytes"] == 222
    # A banked instance holds no live endpoint.
    assert body["published_endpoint"] == nil
    assert body["instance"]["state"] == "banked"
  end

  test "GET /v1/stateful/:name is 404 for an unknown or non-stateful workload" do
    with_stateful_fakes()

    # Not in the catalog.
    assert req(:get, "/v1/stateful/wl-unknown", auth("good")).status == 404
    # A serving-class (not stateful) workload is a 404 on the stateful surface.
    assert req(:get, "/v1/stateful/wl-serving", auth("good")).status == 404
  end

  test "GET /v1/stateful/:name needs management auth" do
    with_stateful_fakes()
    assert req(:get, "/v1/stateful/wl-live").status == 401
  end

  # -- DELETE /v1/stateful/:name/instance (R4) -------------------------------

  test "DELETE /v1/stateful/:name/instance destroys the live instance and evicts the bundle" do
    with_stateful_manager_fake()

    resp = req(:delete, "/v1/stateful/wl-live/instance", auth("good"))
    assert resp.status == 200
    body = json(resp.body)
    assert body["workload"] == "wl-live"
    assert body["destroyed"] == 1
    assert body["evicted"] == 0
  end

  test "DELETE /v1/stateful/:name/instance on an empty workload rolls zero, not a 404" do
    with_stateful_manager_fake()

    resp = req(:delete, "/v1/stateful/wl-empty/instance", auth("good"))
    assert resp.status == 200
    body = json(resp.body)
    assert body["destroyed"] == 0
    assert body["evicted"] == 0
  end

  test "DELETE /v1/stateful/:name/instance needs management auth" do
    with_stateful_manager_fake()
    assert req(:delete, "/v1/stateful/wl-live/instance").status == 401
  end

  # -- DELETE /v1/stateful/:name/volume (R4) ---------------------------------

  test "DELETE /v1/stateful/:name/volume deletes a clean workload's volume" do
    with_stateful_manager_fake()

    resp = req(:delete, "/v1/stateful/wl-clean/volume", auth("good"))
    assert resp.status == 200
    body = json(resp.body)
    assert body["workload"] == "wl-clean"
    assert body["deleted"] == true
  end

  test "DELETE /v1/stateful/:name/volume is REFUSED (409) while an instance exists" do
    with_stateful_manager_fake()

    resp = req(:delete, "/v1/stateful/wl-blocked/volume", auth("good"))
    assert resp.status == 409
    body = json(resp.body)
    assert body["workload"] == "wl-blocked"
    refute body["retryable"]
  end

  test "DELETE /v1/stateful/:name/volume surfaces a store failure as 500" do
    with_stateful_manager_fake()

    resp = req(:delete, "/v1/stateful/wl-boom/volume", auth("good"))
    assert resp.status == 500
  end

  test "DELETE /v1/stateful/:name/volume names nodes when deletion is incomplete" do
    with_stateful_manager_fake()

    resp = req(:delete, "/v1/stateful/wl-incomplete/volume", auth("good"))
    assert resp.status == 500
    body = json(resp.body)
    assert body["error"] == "volume delete incomplete"
    assert body["nodes"] == ["node-2"]
    assert body["retryable"]
  end

  test "DELETE /v1/stateful/:name/volume needs management auth" do
    with_stateful_manager_fake()
    assert req(:delete, "/v1/stateful/wl-clean/volume").status == 401
  end

  describe "GET /v1/conformance" do
    setup do
      Application.put_env(:embervm, :authenticator, FakeAuth)

      # Establish the gate state this block asserts on rather than inheriting
      # it. The gate is a global `:persistent_term`, so before the restores in
      # checker_test and spec_trace_test these cases passed or failed on ExUnit
      # seed alone. Those leaks are fixed, but a test whose precondition is set
      # by whichever module ran first is one edit away from breaking again, and
      # the failure reads as flakiness rather than as a missing on_exit.
      System.put_env("EMBERVM_SPEC_TRACE", "off")
      Embervm.SpecTrace.configure()

      on_exit(fn -> Application.delete_env(:embervm, :authenticator) end)
      :ok
    end

    # THE TEST THAT MATTERS MOST for this endpoint. Both a disabled gate and an
    # empty trace are legitimately VACUOUS, but they must never render alike: an
    # operator reaching for this during an incident is exactly the person who
    # would read "nothing to report" as "the system is conforming". #4758 is the
    # precedent, a gate defaulted off with nothing in the data saying so.
    test "with the trace gate OFF, reports vacuous and says so, never passing" do
      {:ok, resp} =
        Finch.build(:get, "http://127.0.0.1:8080/v1/conformance", [{"authorization", "Bearer good"}])
        |> Finch.request(Embervm.Finch)

      assert resp.status == 200
      body = Jason.decode!(resp.body)

      # Disabled is stated, not inferred from an absence.
      assert body["enabled"] == false

      verdicts = body["verdicts"]
      assert verdicts != [] and verdicts != nil,
             "a disabled gate must still enumerate the invariants, not return an empty list"

      # Every invariant vacuous, NONE passing. A pass here would be a claim about
      # a system nothing observed.
      assert Enum.all?(verdicts, &(&1["verdict"] == "vacuous")),
             "expected all vacuous, got #{inspect(Enum.map(verdicts, & &1["verdict"]))}"

      refute Enum.any?(verdicts, &(&1["verdict"] == "pass"))

      # The reason names the gate, so the caller can tell this from an empty trace.
      assert Enum.all?(verdicts, fn v -> v["detail"] =~ "gate" end),
             "the vacuous reason must name the disabled gate so it is distinguishable from an empty trace"

      assert Enum.map(verdicts, &String.to_existing_atom(&1["invariant"])) ==
               Embervm.SpecTrace.Checker.invariants()
    end

    test "the verdict triple is present, never flattened to pass/fail" do
      {:ok, resp} =
        Finch.build(:get, "http://127.0.0.1:8080/v1/conformance", [{"authorization", "Bearer good"}])
        |> Finch.request(Embervm.Finch)

      body = Jason.decode!(resp.body)
      verdict = List.first(body["verdicts"])

      # coverage and oracle are what stop a green tick being read as more than it
      # checked; a caller must be able to tell "checked 400, all clean" from
      # "checked nothing".
      assert Map.has_key?(verdict, "verdict")
      assert Map.has_key?(verdict, "coverage")
      assert Map.has_key?(verdict, "oracle")
      assert verdict["oracle"] == "trace_only"
    end

    test "requires auth like every other /v1 route" do
      {:ok, resp} =
        Finch.build(:get, "http://127.0.0.1:8080/v1/conformance")
        |> Finch.request(Embervm.Finch)

      assert resp.status == 401
    end
  end
end
