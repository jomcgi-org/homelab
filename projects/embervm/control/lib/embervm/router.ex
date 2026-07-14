defmodule Embervm.Router do
  @moduledoc """
  The control-plane HTTP router (Bandit-hosted, see `Embervm.Application`): the
  submit API and the health endpoint.

  ## Surface (Task 8)

    * `POST /v1/workloads/:name/tasks` submit a task. Async by default (`202` +
      task_id); `?wait=true` parks the request until the task is terminal or the
      sync timeout elapses (a parked BEAM process, per `Embervm.SyncWait`).
    * `GET  /v1/tasks/:id`             task state + metadata (from ETS).
    * `GET  /v1/tasks/:id/result`      the stored result (until its TTL).
    * `GET  /v1/workloads/:name/dead-letters`  paged DLQ listing.
    * `POST /v1/tasks/:id/redrive`     re-queue a dead-lettered task (audited).
    * `GET  /healthz`                  unauthenticated liveness/readiness.

  ## Task envelope

  A task IS one HTTP request to the guest. Method is always POST for the task
  class. The submit body is forwarded to the guest VERBATIM, which is why this
  route does NOT run `Plug.Parsers`: parsing would consume/transform the body.
  The guest path defaults to the workload's `spec.source.invokePath` (hard-coded
  `/` until Task 5 wires the catalog), overridable per submit with
  `X-Ember-Guest-Path`. Request headers prefixed `X-Ember-Guest-` are stripped of
  the prefix and forwarded; no other caller header reaches the guest. The whole
  envelope is captured into the durable `submitted` record so the dispatcher
  (Task 11) can rebuild the guest request; nothing dispatches yet, so in this
  phase submitted tasks simply sit `queued`.

  ## Auth

  Every `/v1` path requires a bearer token authenticated by `Embervm.Auth`
  (TokenReview + allow-list, cached). The reviewer is resolved from application
  env so request tests can inject a fake without a live API server; production
  uses the supervised `Embervm.Auth`. `/healthz` is unauthenticated.
  """
  use Plug.Router

  require Logger

  alias Embervm.{SyncWait, TaskState, TaskStore}

  # Single tenant in v1 (the `homelab` tenant); the column is still carried on
  # every record so multi-tenant is a data change, not a schema change.
  @tenant "homelab"
  # 8 MiB, matching the daemon Assign body cap.
  @max_body 8_388_608
  @guest_header_prefix "x-ember-guest-"
  @path_header "x-ember-guest-path"
  # Default task-record lifetime; Task 5 will source this from the workload's
  # invocation.resultTtlSeconds once the catalog exists.
  @default_task_ttl_ms 86_400_000

  plug(:match)
  plug(:fetch_query)
  plug(:authenticate)
  plug(:dispatch)

  get "/healthz" do
    conn |> put_resp_content_type("text/plain") |> send_resp(200, "ok")
  end

  post "/v1/workloads/:name/tasks" do
    handle_submit(conn, name)
  end

  get "/v1/tasks/:id" do
    handle_get_task(conn, id)
  end

  get "/v1/tasks/:id/result" do
    handle_get_result(conn, id)
  end

  get "/v1/workloads/:name/dead-letters" do
    handle_dead_letters(conn, name)
  end

  post "/v1/tasks/:id/redrive" do
    handle_redrive(conn, id)
  end

  match _ do
    send_resp(conn, 404, "")
  end

  # -- plugs -----------------------------------------------------------------

  defp fetch_query(conn, _opts), do: fetch_query_params(conn)

  # Auth is scoped to /v1: /healthz stays open for the kubelet probes.
  defp authenticate(%Plug.Conn{path_info: ["v1" | _]} = conn, _opts), do: do_authenticate(conn)
  defp authenticate(conn, _opts), do: conn

  defp do_authenticate(conn) do
    authenticator = Application.get_env(:embervm, :authenticator, Embervm.Auth)

    with {:ok, token} <- bearer_token(conn),
         {:ok, principal} <- authenticator.authenticate(token) do
      assign(conn, :principal, principal)
    else
      {:error, :no_token} ->
        halt_json(conn, 401, %{error: "missing bearer token", retryable: false})

      {:error, :forbidden} ->
        halt_json(conn, 403, %{error: "service account not permitted", retryable: false})

      {:error, _reason} ->
        halt_json(conn, 401, %{error: "authentication failed", retryable: true})
    end
  end

  # -- submit ----------------------------------------------------------------

  defp handle_submit(conn, workload) do
    principal = conn.assigns.principal

    # Per-principal queue-depth cap: a synchronous 429 pre-check BEFORE the durable
    # submit (the dispatcher owns the fair-queue depth; the FSM has no queued->fail
    # edge, so a queued task cannot be terminally dropped after the fact). Advisory
    # and coarse: the real reservation happens on enqueue, so a concurrent burst can
    # momentarily overshoot, and a control plane with no dispatcher wired fails
    # closed (denies) rather than admitting unbounded backlog.
    if Embervm.Dispatcher.admit?(workload, principal) do
      submit_admitted(conn, workload, principal)
    else
      send_json(conn, 429, %{
        error: "per-principal queue depth cap exceeded for workload",
        workload: workload,
        retryable: true
      })
    end
  end

  defp submit_admitted(conn, workload, principal) do
    case read_capped_body(conn) do
      {:ok, body, conn} ->
        attrs = %{
          tenant: @tenant,
          principal: principal,
          workload: workload,
          idempotency_key: header_value(conn, "idempotency-key"),
          expires_at: now_ms() + @default_task_ttl_ms,
          request: build_request_env(conn, body)
        }

        case TaskStore.submit(store(), attrs) do
          {:ok, _created_or_existing, task_id} ->
            respond_submit(conn, task_id, principal)

          {:error, reason} ->
            Logger.error("embervm submit failed: #{inspect(reason)}")
            send_json(conn, 500, %{error: "submit failed", retryable: true})
        end

      {:error, :too_large} ->
        send_json(conn, 413, %{error: "request body exceeds 8 MiB", retryable: false})
    end
  end

  defp respond_submit(conn, task_id, principal) do
    if sync?(conn) do
      sync_respond(conn, task_id, principal)
    else
      send_json(conn, 202, %{task_id: task_id, state: state_string(task_id)})
    end
  end

  # Sync submit: reserve a park slot (fail-closed 429 past the per-principal cap),
  # then park until terminal or timeout. On wake, re-read TaskStore for the
  # AUTHORITATIVE state: the permanent-failure path notifies on failed_permanent
  # before chaining to dead_lettered, so the woken state may be intermediate; the
  # re-read (serialized behind TaskStore's in-flight transition) always sees the
  # settled state.
  defp sync_respond(conn, task_id, principal) do
    cap = Application.get_env(:embervm, :sync_park_cap, 512)
    timeout = Application.get_env(:embervm, :sync_timeout_ms, 90_000)

    case SyncWait.reserve(principal, cap) do
      :ok ->
        try do
          case SyncWait.await(task_id, timeout, fn -> terminal_check(task_id) end) do
            {:terminal, _state} -> respond_terminal(conn, task_id)
            :timeout -> send_json(conn, 202, %{task_id: task_id, state: state_string(task_id)})
          end
        after
          SyncWait.release(principal)
        end

      {:error, :park_cap_exceeded} ->
        send_json(conn, 429, %{
          error: "sync wait cap exceeded for principal",
          task_id: task_id,
          retryable: true
        })
    end
  end

  defp terminal_check(task_id) do
    case TaskStore.get(store(), task_id) do
      {:ok, %{state: state}} ->
        if TaskState.terminal?(state), do: {:terminal, state}, else: :pending

      _ ->
        :pending
    end
  end

  # In R0 there is no dispatcher, so this is only reached in tests that drive a
  # task terminal directly. When Task 11 lands, the dispatcher hands the LIVE
  # untruncated guest response to the parked caller; until then, a succeeded task
  # is answered from the stored (possibly truncated) result copy.
  defp respond_terminal(conn, task_id) do
    case TaskStore.get(store(), task_id) do
      {:ok, %{state: :succeeded}} ->
        case TaskStore.get_result(store(), task_id) do
          {:ok, %{status_code: code, body: body, truncated: truncated}} ->
            conn
            |> put_resp_header("x-ember-truncated", to_string(truncated))
            |> send_guest_result(code, body)

          {:ok, nil} ->
            send_json(conn, 200, %{task_id: task_id, state: "succeeded"})

          {:error, _} ->
            send_json(conn, 500, %{error: "result read failed", task_id: task_id, retryable: true})
        end

      {:ok, %{state: state}} ->
        send_json(conn, 502, %{
          error: "task did not succeed",
          task_id: task_id,
          state: to_string(state),
          retryable: false
        })

      :error ->
        send_json(conn, 404, %{error: "task not found", task_id: task_id, retryable: false})
    end
  end

  # -- reads -----------------------------------------------------------------

  defp handle_get_task(conn, task_id) do
    case TaskStore.get(store(), task_id) do
      {:ok, task} -> send_json(conn, 200, task_view(task))
      :error -> send_json(conn, 404, %{error: "task not found", task_id: task_id, retryable: false})
    end
  end

  defp handle_get_result(conn, task_id) do
    case TaskStore.get_result(store(), task_id) do
      {:ok, %{status_code: code, body: body, truncated: truncated}} ->
        conn
        |> put_resp_header("x-ember-truncated", to_string(truncated))
        |> send_guest_result(code, body)

      {:ok, nil} ->
        send_json(conn, 404, %{
          error: "no result (task never ran or the result TTL expired)",
          task_id: task_id,
          retryable: false
        })

      {:error, _} ->
        send_json(conn, 500, %{error: "result read failed", task_id: task_id, retryable: true})
    end
  end

  defp handle_dead_letters(conn, workload) do
    limit = conn |> int_param("limit", 50) |> clamp(1, 500)
    offset = conn |> int_param("offset", 0) |> max(0)

    {:ok, page} = TaskStore.list_dead_letters(store(), workload, limit: limit, offset: offset)

    send_json(conn, 200, %{
      workload: workload,
      items: Enum.map(page.items, &task_view/1),
      total: page.total,
      limit: page.limit,
      offset: page.offset
    })
  end

  defp handle_redrive(conn, task_id) do
    case TaskStore.redrive(store(), task_id) do
      {:ok, task} ->
        send_json(conn, 200, task_view(task))

      {:error, {:not_found, _}} ->
        send_json(conn, 404, %{error: "task not found", task_id: task_id, retryable: false})

      {:error, {:illegal_transition, _, _}} ->
        send_json(conn, 409, %{
          error: "task is not dead-lettered; only dead-lettered tasks can be redriven",
          task_id: task_id,
          retryable: false
        })

      {:error, _reason} ->
        send_json(conn, 500, %{error: "redrive failed", task_id: task_id, retryable: true})
    end
  end

  # -- request helpers -------------------------------------------------------

  defp bearer_token(conn) do
    case get_req_header(conn, "authorization") do
      ["Bearer " <> token | _] -> {:ok, token}
      ["bearer " <> token | _] -> {:ok, token}
      _ -> {:error, :no_token}
    end
  end

  # Reads the body with an 8 MiB cap: read_body returns {:more, ...} when the
  # body exceeds :length, which we map to 413 rather than silently truncating.
  defp read_capped_body(conn) do
    case read_body(conn, length: @max_body, read_length: @max_body) do
      {:ok, body, conn} -> {:ok, body, conn}
      {:more, _partial, _conn} -> {:error, :too_large}
      {:error, _} = error -> error
    end
  end

  # The guest-request envelope stored verbatim for the dispatcher. Only present
  # fields are included so op-log JSON never carries a null (nested nils are not
  # stripped by the encoder).
  defp build_request_env(conn, body) do
    base = %{path: guest_path(conn), headers: guest_headers(conn), body_b64: Base.encode64(body)}

    case header_value(conn, "content-type") do
      nil -> base
      ct -> Map.put(base, :content_type, ct)
    end
  end

  defp guest_path(conn) do
    # Default is the workload's spec.source.invokePath; hard-coded `/` until the
    # Task 5 catalog exists. X-Ember-Guest-Path overrides per submit.
    header_value(conn, @path_header) || "/"
  end

  defp guest_headers(conn) do
    conn.req_headers
    |> Enum.filter(fn {k, _v} ->
      String.starts_with?(k, @guest_header_prefix) and k != @path_header
    end)
    |> Map.new(fn {k, v} -> {String.replace_prefix(k, @guest_header_prefix, ""), v} end)
  end

  defp header_value(conn, name) do
    case get_req_header(conn, name) do
      [value | _] -> value
      [] -> nil
    end
  end

  defp sync?(conn), do: Map.get(conn.query_params, "wait") == "true"

  defp int_param(conn, name, default) do
    case Map.get(conn.query_params, name) do
      nil ->
        default

      raw ->
        case Integer.parse(raw) do
          {n, _} -> n
          :error -> default
        end
    end
  end

  defp clamp(n, lo, hi), do: n |> max(lo) |> min(hi)

  # -- responses -------------------------------------------------------------

  defp task_view(task) do
    %{
      task_id: task.task_id,
      workload: task.workload,
      principal: task.principal,
      state: to_string(task.state),
      attempt: task.attempt,
      submitted_at: task.submitted_at,
      updated_at: task.updated_at
    }
  end

  defp state_string(task_id) do
    case TaskStore.get(store(), task_id) do
      {:ok, %{state: state}} -> to_string(state)
      :error -> "unknown"
    end
  end

  defp send_guest_result(conn, status_code, body) do
    conn
    |> put_resp_content_type("application/octet-stream")
    |> send_resp(status_code, body || "")
  end

  defp send_json(conn, status, map) do
    conn
    |> put_resp_content_type("application/json")
    |> send_resp(status, encode_json(map))
  end

  defp halt_json(conn, status, map) do
    conn |> send_json(status, map) |> halt()
  end

  defp encode_json(map), do: map |> :json.encode() |> :erlang.iolist_to_binary()

  # The submit API reads/writes only through TaskStore (ETS + result store),
  # never the op-log internals. The store is resolvable from app env for symmetry
  # with the authenticator, defaulting to the supervised singleton.
  defp store, do: Application.get_env(:embervm, :task_store, Embervm.TaskStore)

  defp now_ms, do: System.system_time(:millisecond)
end
