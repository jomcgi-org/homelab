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
    * `GET  /v1/nodes`                 read-only node health + dispatcher snapshot
      (operational introspection: capacity facts, inventory, denials).
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
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{SessionTrace, SyncWait, TaskState, TaskStore}

  # Single tenant in v1 (the `homelab` tenant); the column is still carried on
  # every record so multi-tenant is a data change, not a schema change.
  @tenant "homelab"
  # 8 MiB, matching the daemon Assign body cap.
  @max_body 8_388_608
  @guest_header_prefix "x-ember-guest-"
  @path_header "x-ember-guest-path"
  # The header the serving route injects so the activator resolves the missed
  # workload without parsing hosts (standing decision 3). Its presence on an
  # otherwise-unmatched request is what marks the request as a serving miss.
  @workload_header "x-ember-workload"
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

  get "/v1/usage" do
    handle_usage(conn)
  end

  get "/v1/nodes" do
    handle_nodes(conn)
  end

  # POST /v1/nodes/register (NODE auth ONLY): the dial-home registration a noded
  # instance POSTs on start and on a jittered interval, advertising its identity
  # {node, pod_uid, address, boot_id} so the control plane adopts it without ever
  # listing pods (ADR embervm/005, R0 PR-2). Authenticated in-handler against the
  # noded ServiceAccount (NOT the task-submit allow-list), so a node can register
  # without being able to submit tasks.
  post "/v1/nodes/register" do
    handle_node_register(conn)
  end

  post "/v1/tasks/:id/redrive" do
    handle_redrive(conn, id)
  end

  # -- session routes (R2, front-end only; no placement logic here) ----------

  post "/v1/workloads/:name/sessions" do
    handle_create_session(conn, name)
  end

  get "/v1/workloads/:name/sessions" do
    handle_list_sessions(conn, name)
  end

  post "/v1/sessions/:id/invoke" do
    handle_session_invoke(conn, id)
  end

  get "/v1/sessions/:id" do
    handle_get_session(conn, id)
  end

  delete "/v1/sessions/:id" do
    handle_destroy_session(conn, id)
  end

  # -- serving routes (R3, management introspection) -------------------------

  get "/v1/serving/:name" do
    handle_get_serving(conn, name)
  end

  # DELETE /v1/serving/:name/instances (management auth): the forced roll. Drains and
  # DESTROYS every live instance of the workload (and evicts its banked snapshots) so
  # the next miss cold-creates on the CURRENT base. Returns the counts destroyed +
  # evicted. Behind the same /v1 management auth as the GET.
  delete "/v1/serving/:name/instances" do
    handle_force_roll(conn, name)
  end

  # -- stateful routes (R4, management introspection) ------------------------

  get "/v1/stateful/:name" do
    handle_get_stateful(conn, name)
  end

  # DELETE /v1/stateful/:name/instance (management auth): destroy the live
  # instance AND evict the banked bundle, so the next connection cold-boots the
  # current image against the still-intact volume. Returns the counts destroyed
  # + evicted (each 0 or 1, the class is a singleton).
  delete "/v1/stateful/:name/instance" do
    handle_destroy_stateful_instance(conn, name)
  end

  # DELETE /v1/stateful/:name/volume (management auth): the ONLY destructive
  # data verb. REFUSED (409) while any non-terminal instance exists for the
  # workload; a CR deletion never reaches this, deletion is always this
  # explicit act.
  delete "/v1/stateful/:name/volume" do
    handle_delete_stateful_volume(conn, name)
  end

  # POST /v1/stateful/:name/handover/:target (management auth): move the
  # workload's VOLUME off its current anchor node onto :target and re-anchor it
  # there (#4119 slice 4), so a workload whose anchor cannot host its wake stops
  # being pinned to a node that will never have room. REFUSED (409) while any
  # live instance exists: the move is defined only for a banked workload,
  # because moving a volume out from under a writable attach is exactly the
  # split brain the single-writer fence exists to prevent.
  #
  # The target is a PATH segment rather than a body field so the verb needs no
  # request parser and stays drillable with a bare curl, which matters for a
  # manual-trigger verb whose whole point is being rehearsed by hand before
  # anything automates it.
  post "/v1/stateful/:name/handover/:target" do
    handle_stateful_handover(conn, name, target)
  end

  # -- composite (group) routes (R5, management introspection) ---------------

  get "/v1/groups/:name" do
    handle_get_group(conn, name)
  end

  # DELETE /v1/groups/:name/instance (management auth): the forced roll. Destroys
  # every live member + deletes the network + evicts the banked set, KEEPING the
  # workload definition so the next connection fresh-boots a new environment on the
  # current images. The convergence + degraded-recovery lever. Returns the counts
  # destroyed + evicted (each 0 or 1, the class is a group-level singleton).
  delete "/v1/groups/:name/instance" do
    handle_force_roll_group(conn, name)
  end

  # The activator fallback (R3, Task 8): the catch-all is the front-end the node
  # Envoy routes a MISS to (the fallback endpoint of an empty serve|<workload>
  # cluster). It is identified by the `x-ember-workload` request header the serving
  # route injects; a request carrying that header IS a miss signal for that
  # workload (standing decision 1). Any OTHER unmatched path is a genuine 404. This
  # is deliberately the last route so it never shadows /healthz, /v1/*, or the
  # management surface: only a request that matched nothing above AND carries the
  # activator header reaches the miss path.
  match _ do
    case header_value(conn, @workload_header) do
      workload when is_binary(workload) and workload != "" ->
        handle_activator_miss(conn, workload)

      _ ->
        send_resp(conn, 404, "")
    end
  end

  # -- plugs -----------------------------------------------------------------

  defp fetch_query(conn, _opts), do: fetch_query_params(conn)

  # Auth is scoped to /v1: /healthz stays open for the kubelet probes. The session
  # routes that take a SESSION token (invoke, and the session-token-or-management
  # GET) authenticate in their handler, not here, because the bearer token they
  # carry is a per-session capability, not a ServiceAccount token TokenReview would
  # recognize. Running management auth on them would 401 every valid session token.
  # So this plug runs management auth on every /v1 path EXCEPT those, which
  # `session_token_route?/1` names explicitly (a closed allow-list, not a prefix,
  # so a new management route is never accidentally opened).
  defp authenticate(%Plug.Conn{path_info: ["v1" | _]} = conn, _opts) do
    if in_handler_auth_route?(conn), do: conn, else: do_authenticate(conn)
  end

  defp authenticate(conn, _opts), do: conn

  # Routes that authenticate IN-HANDLER rather than via the management-auth plug,
  # because their bearer token is not a task-submit ServiceAccount token: the
  # session-token routes (verified against Embervm.SessionStore) and the node
  # dial-home registration (verified against the noded ServiceAccount, NOT the
  # submit allow-list). Running management auth on them would 401 a valid token.
  defp in_handler_auth_route?(conn), do: session_token_route?(conn) or node_register_route?(conn)

  defp node_register_route?(%Plug.Conn{method: "POST", path_info: ["v1", "nodes", "register"]}), do: true
  defp node_register_route?(_conn), do: false

  # The routes whose bearer token is a SESSION token (verified in-handler against
  # Embervm.SessionStore), NOT a management ServiceAccount token: POST
  # /v1/sessions/:id/invoke (session token ONLY) and GET /v1/sessions/:id
  # (management OR session token). Matched structurally on path_info so a query
  # string or trailing content cannot smuggle a management route past the gate.
  defp session_token_route?(%Plug.Conn{method: "POST", path_info: ["v1", "sessions", _id, "invoke"]}),
    do: true

  defp session_token_route?(%Plug.Conn{method: "GET", path_info: ["v1", "sessions", _id]}), do: true
  defp session_token_route?(_conn), do: false

  defp do_authenticate(conn) do
    authenticator = Application.get_env(:embervm, :authenticator, Embervm.Auth)

    with {:ok, token} <- bearer_token(conn),
         {:ok, principal} <- authenticator.authenticate(token) do
      assign(conn, :principal, principal)
    else
      {:error, :no_token} ->
        # Unauthenticated: deliberately NOT appended. Each append is an fsync, and
        # letting an unauthenticated caller force durable writes is a
        # write-amplification vector against the single-writer op-log.
        halt_json(conn, 401, %{error: "missing bearer token", retryable: false})

      {:error, {:forbidden, username}} ->
        # Authenticated but not allow-listed: a genuine, principal-named audit
        # event, appended once per rejected request.
        Embervm.Metering.record_denial(username, nil, :forbidden)
        halt_json(conn, 403, %{error: "service account not permitted", retryable: false})

      {:error, :forbidden} ->
        # A reviewer (or test fake) that does not surface the username: still a
        # 403 audit event, principal unknown.
        Embervm.Metering.record_denial(nil, nil, :forbidden)
        halt_json(conn, 403, %{error: "service account not permitted", retryable: false})

      {:error, _reason} ->
        halt_json(conn, 401, %{error: "authentication failed", retryable: true})
    end
  end

  # -- submit ----------------------------------------------------------------

  defp handle_submit(conn, workload) do
    principal = conn.assigns.principal

    # Two synchronous pre-checks BEFORE the durable submit, both because the FSM
    # has no queued->failed edge (a queued task cannot be terminally dropped after
    # the fact, so an unadmittable request must be rejected before it becomes one):
    #
    #   * queue-depth cap (dispatcher-owned): coarse per-principal abuse guard; the
    #     real reservation happens on enqueue, so a concurrent burst can momentarily
    #     overshoot, and a control plane with no dispatcher wired fails closed.
    #   * daily vCPU-second quota (fail-closed): rejects a principal already at
    #     budget so it cannot fill its own queue with tasks that would only park at
    #     dispatch. This is the courtesy fast-fail; the dispatcher's rotation skip is
    #     the actual enforcement point for a principal that goes over mid-flight.
    #
    # Each denial is appended once here (an authenticated, request-bounded audit
    # record), not per dispatch tick.
    cond do
      not Embervm.Dispatcher.admit?(workload, principal) ->
        Embervm.Metering.record_denial(principal, workload, :queue_depth)

        send_json(conn, 429, %{
          error: "per-principal queue depth cap exceeded for workload",
          workload: workload,
          retryable: true
        })

      not Embervm.Metering.within_quota?(principal) ->
        Embervm.Metering.record_denial(principal, workload, :quota)

        send_json(conn, 429, %{
          error: "daily vCPU-second quota exhausted for principal",
          workload: workload,
          retryable: true
        })

      true ->
        submit_admitted(conn, workload, principal)
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
          {:ok, %{status_code: code, body: body, truncated: truncated} = result} ->
            conn
            |> put_resp_header("x-ember-truncated", to_string(truncated))
            |> send_guest_result(code, body, Map.get(result, :headers, %{}))

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
      {:ok, %{status_code: code, body: body, truncated: truncated} = result} ->
        conn
        |> put_resp_header("x-ember-truncated", to_string(truncated))
        |> send_guest_result(code, body, Map.get(result, :headers, %{}))

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

  # GET /v1/usage: paged per-(principal, day) billed usage from the metering
  # projection. Self-scoped by default (a caller sees only its own principal); a
  # values-configured admin (usage_admins) may pass ?principal= to read one
  # principal, or omit it to read all. `since` is a wall-clock ms floor, mapped to
  # the projection's epoch-day bucket.
  defp handle_usage(conn) do
    principal = conn.assigns.principal
    admin? = principal in Application.get_env(:embervm, :usage_admins, [])

    filter_principal =
      if admin?, do: Map.get(conn.query_params, "principal"), else: principal

    since_day =
      case int_param(conn, "since", nil) do
        nil -> 0
        ms -> div(ms, 86_400_000)
      end

    limit = conn |> int_param("limit", 100) |> clamp(1, 1000)
    offset = conn |> int_param("offset", 0) |> max(0)

    opts =
      [since_day: since_day, limit: limit, offset: offset] ++
        if(filter_principal, do: [principal: filter_principal], else: [])

    {:ok, page} = TaskStore.list_usage(store(), opts)

    send_json(conn, 200, %{
      items: Enum.map(page.items, &usage_view/1),
      total: page.total,
      limit: page.limit,
      offset: page.offset
    })
  end

  defp usage_view(row) do
    %{
      principal: row.principal,
      day: row.day,
      vcpu_seconds: row.vcpu_seconds,
      gb_seconds: row.gb_seconds,
      task_count: row.task_count,
      updated_at: row.updated_at
    }
  end

  # Read-only operational introspection. Added after the R0 dispatch-recovery
  # drill, which was slow to diagnose precisely because there was no way to see
  # live NodeRegistry/Dispatcher state (the release is not distributed, so no IEx
  # remote, and there was no debug endpoint). Returns each configured node's health
  # and capacity facts (including primed_vm_ids, the seam of the adoption fix) plus
  # the dispatcher's queue/inventory/denial snapshot. Behind the same /v1 auth as
  # every other route; a slow/failed status call degrades to an empty snapshot
  # rather than failing the request.
  defp handle_nodes(conn) do
    send_json(conn, 200, %{nodes: nodes_snapshot(), dispatch: dispatch_snapshot()})
  end

  defp nodes_snapshot do
    Embervm.NodeRegistry.status()
    |> Enum.map(fn {node_id, s} ->
      %{
        node_id: node_id,
        health: s.health,
        draining: s.draining,
        dispatchable: s.dispatchable,
        connected: s.connected,
        address: s.address,
        facts: node_facts_view(s.facts)
      }
    end)
  rescue
    _ -> []
  catch
    _, _ -> []
  end

  defp node_facts_view(nil), do: nil

  defp node_facts_view(f) do
    %{
      live_vms: Map.get(f, :live_vms),
      max_live_vms: Map.get(f, :max_live_vms),
      mem_headroom_mib: Map.get(f, :mem_headroom_mib),
      updated_at: Map.get(f, :updated_at),
      workloads:
        for {wl, wc} <- Map.get(f, :workloads, %{}), into: %{} do
          {wl,
           %{
             free_primed_slots: Map.get(wc, :free_primed_slots),
             base_state: Map.get(wc, :base_state),
             snapshot_ref: Map.get(wc, :snapshot_ref),
             primed_vm_ids: Map.get(wc, :primed_vm_ids, [])
           }}
        end
    }
  end

  defp dispatch_snapshot do
    s = Embervm.Dispatcher.stats()

    %{
      denials: s.denials,
      warm_hits: s.warm_hits,
      misses: s.misses,
      queued: s.queued,
      workers: s.workers,
      queue_depth: s.queue_depth,
      # inventory is keyed by {node_id, workload}; stringify the tuple key for JSON.
      inventory: for({{node, wl}, len} <- s.inventory, into: %{}, do: {"#{node}/#{wl}", len})
    }
  rescue
    _ -> nil
  catch
    _, _ -> nil
  end

  # -- node dial-home registration (R0 PR-2) ---------------------------------

  # POST /v1/nodes/register. Authenticates the caller as the noded ServiceAccount
  # (a valid TokenReview whose username matches the configured noded SA), NOT via
  # the task-submit allow-list, then upserts the instance in the NodeRegistry.
  # Registration is advertisement, so a valid-but-malformed body is accepted as a
  # benign no-op (200) rather than 400: the daemon keeps re-advertising and the
  # WatchNode stream is the real liveness signal. A missing/invalid token is 401.
  defp handle_node_register(conn) do
    case authorize_node(conn) do
      :ok ->
        case read_capped_body(conn) do
          {:ok, body, conn} ->
            reg = decode_registration(body)
            _ = Embervm.NodeRegistry.register(reg)
            send_json(conn, 200, %{registered: true})

          {:error, :too_large} ->
            send_json(conn, 413, %{error: "registration body too large", retryable: false})

          {:error, _} ->
            send_json(conn, 400, %{error: "could not read registration body", retryable: true})
        end

      {:error, status, payload} ->
        send_json(conn, status, payload)
    end
  end

  # Node auth: the bearer token must TokenReview to a ServiceAccount username that
  # equals the configured noded SA (:noded_service_account app env, rendered from
  # the chart). We accept both {:ok, username} (the SA also happens to be
  # submit-allow-listed) and {:error, {:forbidden, username}} (a valid token that
  # is simply not on the submit allow-list, the normal case for a node), because
  # node identity is orthogonal to task-submit rights. An unset configured SA
  # ("") accepts ANY valid ServiceAccount token (a permissive fallback for a
  # cluster that has not pinned the SA yet); it never accepts an invalid token.
  defp authorize_node(conn) do
    authenticator = Application.get_env(:embervm, :authenticator, Embervm.Auth)
    expected = Application.get_env(:embervm, :noded_service_account, "")

    with {:ok, token} <- bearer_token(conn),
         {:ok, username} <- node_username(authenticator.authenticate(token)) do
      if expected == "" or username == expected do
        :ok
      else
        {:error, 403, %{error: "not the noded service account", retryable: false}}
      end
    else
      {:error, :no_token} ->
        {:error, 401, %{error: "missing bearer token", retryable: false}}

      _ ->
        {:error, 401, %{error: "node authentication failed", retryable: true}}
    end
  end

  # Both an allow-listed success and a forbidden-but-verified result carry the
  # TokenReview username, which is all node auth needs; every other shape is a
  # genuine auth failure.
  defp node_username({:ok, username}), do: {:ok, username}
  defp node_username({:error, {:forbidden, username}}), do: {:ok, username}
  defp node_username(_), do: :error

  # Decode the registration JSON into a string-keyed map, tolerating a malformed
  # body (yields %{} the registry rejects as invalid, still a benign 200).
  defp decode_registration(body) do
    case safe_decode(body) do
      %{} = map -> map
      _ -> %{}
    end
  end

  defp safe_decode(""), do: %{}

  defp safe_decode(body) do
    :json.decode(body)
  rescue
    _ -> %{}
  catch
    _, _ -> %{}
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

  # -- session handlers (R2) -------------------------------------------------

  # POST /v1/workloads/:name/sessions (management auth). Delegates to the
  # SessionManager, which owns capacity/quota/placement/claim. 201 with the token
  # (returned ONCE), 429 for a capacity/quota denial, 403 for a class mismatch or
  # unknown workload, 500 for an internal failure.
  defp handle_create_session(conn, workload) do
    principal = conn.assigns.principal

    case session_manager().create(session_manager_server(), workload, principal) do
      {:ok, created} ->
        send_json(conn, 201, %{
          session_id: created.session_id,
          session_token: created.token,
          expires_at: created.expires_at,
          base_digest: created.base_digest,
          state: to_string(Map.get(created, :state, :running))
        })

      {:error, {:denied, reason}} ->
        create_denial(conn, workload, reason)

      {:error, reason} ->
        Logger.error("embervm create session failed: #{inspect(reason)}")
        send_json(conn, 500, %{error: "create session failed", workload: workload, retryable: true})
    end
  end

  # Structured, distinguishable create denials: capacity is 429 (retryable, the
  # caller may retry when a session frees), class/workload errors are 403/404
  # (non-retryable, the request is wrong). The reason string is machine-readable.
  defp create_denial(conn, workload, reason) do
    case reason do
      r when r in [:session_cap, :workload_cap, :quota, :no_capacity] ->
        send_json(conn, 429, %{
          error: "session create denied",
          reason: to_string(r),
          workload: workload,
          retryable: true
        })

      :unknown_workload ->
        send_json(conn, 404, %{error: "unknown workload", reason: "unknown_workload", workload: workload, retryable: false})

      :not_session_class ->
        send_json(conn, 403, %{
          error: "workload is not class session",
          reason: "not_session_class",
          workload: workload,
          retryable: false
        })

      # Brick capacity (PR-3): no brick of the workload's size class has room and
      # the class is flagged fleet-full (desired outran registered past the dwell),
      # so placement is TERMINALLY denied rather than parked. 503 (not the 429 the
      # soft caps use): on the fixed homelab this is a hard capacity wall; on EKS
      # Karpenter adds a node, so the client may retry after backing off.
      :fleet_full ->
        send_json(conn, 503, %{
          error: "fleet at capacity for the workload's size class",
          reason: "fleet_full",
          workload: workload,
          retryable: true
        })

      other ->
        send_json(conn, 500, %{error: "session create failed", reason: inspect(other), workload: workload, retryable: true})
    end
  end

  # GET /v1/workloads/:name/sessions (management auth): paged listing.
  defp handle_list_sessions(conn, workload) do
    limit = conn |> int_param("limit", 50) |> clamp(1, 500)
    offset = conn |> int_param("offset", 0) |> max(0)

    {:ok, page} = session_store().list(session_store_server(), workload, limit: limit, offset: offset)

    send_json(conn, 200, %{
      workload: workload,
      items: Enum.map(page.items, &session_view/1),
      total: page.total,
      limit: page.limit,
      offset: page.offset
    })
  end

  # POST /v1/sessions/:id/invoke (SESSION TOKEN auth ONLY). The bearer token must
  # authenticate THIS session id (a management token is rejected: it is not a
  # session token, so the hash compare fails). Proxies the guest request under the
  # task-envelope allow-list + 8 MiB cap via the SessionManager, and returns the
  # guest response VERBATIM including headers.
  defp handle_session_invoke(conn, session_id) do
    # Restore the CALLER's trace context (the traceparent their httpx/OTel client
    # carried), then open the session-invoke ROOT span so `auth`, `queue_wait`,
    # `relight`, and `guest_exec` all nest under it AND under the caller's trace
    # (the R0 "guilty phase must never be uninstrumented" rule, Task 9). Same
    # `from_remote_span` idiom the dispatcher uses; see Embervm.SessionTrace.
    SessionTrace.restore_parent(header_value(conn, "traceparent"))

    Tracer.with_span "embervm.session.invoke", %{
      attributes: %{"ember.session_id" => session_id}
    } do
      with {:ok, token} <- bearer_token(conn),
           {:ok, session} <- verify_session_token_span(session_id, token) do
        Tracer.set_attributes(%{
          "ember.workload" => Map.get(session, :workload),
          "ember.principal" => Map.get(session, :principal),
          "ember.session_state" => to_string(Map.get(session, :state))
        })

        proxy_invoke(conn, session_id)
      else
        {:error, :no_token} ->
          halt_json(conn, 401, %{error: "missing session token", retryable: false})

        {:error, :terminal} ->
          # A valid token on a terminal session: 410 with the recorded reason.
          session_gone(conn, session_id)

        {:error, _} ->
          halt_json(conn, 403, %{error: "invalid session token", session_id: session_id, retryable: false})
      end
    end
  end

  defp proxy_invoke(conn, session_id) do
    case read_capped_body(conn) do
      {:ok, body, conn} ->
        req = %{
          method: "POST",
          path: guest_path(conn),
          headers: guest_headers(conn),
          body: body,
          # Serialize the invoke ROOT span so the downstream queue_wait/relight/
          # guest_exec spans (which run in other processes across GenServer.call and
          # spawn boundaries, where the OTel process context does not follow) nest
          # under it. nil when tracing is off (CI). See Embervm.SessionTrace.
          traceparent: SessionTrace.current_traceparent()
        }

        case session_manager().invoke(session_manager_server(), session_id, req) do
          {:ok, %{status_code: code, headers: headers, body: resp_body}} ->
            send_guest_result(conn, code, resp_body, headers)

          {:error, {:gone, reason}} ->
            send_json(conn, 410, %{error: "session gone", reason: to_string(reason), session_id: session_id, retryable: false})

          {:error, {:not_ready, state}} ->
            send_json(conn, 409, %{
              error: "session not ready",
              state: to_string(state),
              session_id: session_id,
              retryable: true
            })

          {:error, :queue_full} ->
            send_json(conn, 429, %{
              error: "session invoke queue is full",
              session_id: session_id,
              retryable: true
            })

          {:error, :wake_rate_limited} ->
            # The per-principal wake-rate limit (relight-triggering invokes) tripped:
            # 429 WITHOUT having touched the node (the asymmetric-cost relight was
            # never issued). Retryable once the window drains.
            send_json(conn, 429, %{
              error: "session wake-rate limit exceeded",
              reason: "wake_rate",
              session_id: session_id,
              retryable: true
            })

          {:error, :not_found} ->
            send_json(conn, 404, %{error: "session not found", session_id: session_id, retryable: false})

          {:error, reason} ->
            # A daemon transport/timeout failure: the session is now failed. 502 so
            # the caller knows the invoke did not complete (at-most-once: it is NOT
            # retried by the platform; the caller creates a fresh session).
            send_json(conn, 502, %{error: "session invoke failed", reason: inspect(reason), session_id: session_id, retryable: false})
        end

      {:error, :too_large} ->
        send_json(conn, 413, %{error: "request body exceeds 8 MiB", retryable: false})
    end
  end

  defp guest_path(conn) do
    # Only an EXPLICIT X-Ember-Guest-Path sets the guest path; absent, return nil so
    # the session process falls back to the workload's invokePath (the shim serves
    # only /invoke, so a baked "/" default 404s the guest, the R1 baked-path trap).
    header_value(conn, @path_header)
  end

  # GET /v1/sessions/:id (management OR session token). The management-auth plug is
  # skipped for this route, so authorize here: either a valid management bearer
  # (TokenReview) OR the session's own token. Returns state, generation, base
  # digest, timestamps, expires_at.
  defp handle_get_session(conn, session_id) do
    case authorize_session_read(conn, session_id) do
      :ok ->
        case session_store().get(session_store_server(), session_id) do
          {:ok, session} -> send_json(conn, 200, session_view(session))
          :error -> send_json(conn, 404, %{error: "session not found", session_id: session_id, retryable: false})
        end

      {:error, status, body} ->
        send_json(conn, status, body)
    end
  end

  # DELETE /v1/sessions/:id (management auth): destroy.
  defp handle_destroy_session(conn, session_id) do
    case session_manager().destroy(session_manager_server(), session_id) do
      {:ok, _} -> send_json(conn, 200, %{session_id: session_id, state: "destroyed"})
      {:error, :not_found} -> send_json(conn, 404, %{error: "session not found", session_id: session_id, retryable: false})
      {:error, reason} -> send_json(conn, 500, %{error: "destroy failed", reason: inspect(reason), session_id: session_id, retryable: true})
    end
  end

  # Session read is allowed for a valid management token OR the session's own token.
  # Try the session token first (cheap, in-process hash compare), then fall back to
  # a management TokenReview so an operator can read any session.
  defp authorize_session_read(conn, session_id) do
    case bearer_token(conn) do
      {:ok, token} ->
        case verify_session_token(session_id, token) do
          {:ok, _} ->
            :ok

          {:error, :terminal} ->
            # The token is this session's, but the session is terminal: reads of a
            # terminal session are still allowed (state/reason are exactly what the
            # caller needs), so authorize.
            :ok

          {:error, _} ->
            authorize_management_read(conn, token)
        end

      {:error, :no_token} ->
        {:error, 401, %{error: "missing bearer token", session_id: session_id, retryable: false}}
    end
  end

  defp authorize_management_read(_conn, token) do
    authenticator = Application.get_env(:embervm, :authenticator, Embervm.Auth)

    case authenticator.authenticate(token) do
      {:ok, _principal} -> :ok
      _ -> {:error, 403, %{error: "not authorized to read session", retryable: false}}
    end
  end

  # Verify a session token against Embervm.SessionStore, mapping to the handler's
  # `{:ok, session} | {:error, :terminal | :unauthorized | :not_found}`.
  defp verify_session_token(session_id, token) do
    session_store().verify_token(session_store_server(), session_id, token)
  end

  # The `auth` child span (Task 9): the session-token verify is the invoke's auth
  # phase, kept as its own span so the Task 11 gates can attribute auth latency
  # (mirrors the dispatcher's embervm.auth span for TokenReview).
  defp verify_session_token_span(session_id, token) do
    Tracer.with_span "embervm.session.auth", %{attributes: %{"ember.session_id" => session_id}} do
      verify_session_token(session_id, token)
    end
  end

  defp session_gone(conn, session_id) do
    reason =
      case session_store().get(session_store_server(), session_id) do
        {:ok, %{terminal_reason: r}} when is_binary(r) -> r
        _ -> "gone"
      end

    send_json(conn, 410, %{error: "session gone", reason: reason, session_id: session_id, retryable: false})
  end

  # -- serving handler (R3) --------------------------------------------------

  # GET /v1/serving/:name (management auth): the serving workload's instances,
  # their states, the published endpoints currently in the fan-out, and the
  # publisher's generation (the per-node xDS version counter). Read-only
  # operational introspection, behind the same /v1 management auth as every other
  # non-session route. A workload with no instances returns an empty list (200),
  # not a 404: the workload may be validly defined-but-cold.
  defp handle_get_serving(conn, workload) do
    instances = serving_store().list(serving_store_server(), workload)
    published = Enum.filter(instances, &(&1.state == :published and &1.healthy))

    send_json(conn, 200, %{
      workload: workload,
      instances: Enum.map(instances, &serving_instance_view/1),
      published_endpoints: Enum.map(published, &%{ip: &1.ip, port: &1.port}),
      generation: serving_generation(workload)
    })
  end

  defp serving_instance_view(instance) do
    %{
      instance_id: instance.instance_id,
      workload: instance.workload,
      state: to_string(instance.state),
      healthy: instance.healthy,
      node_id: instance.node_id,
      ip: instance.ip,
      port: instance.port,
      generation: instance.generation,
      created_at: instance.created_at,
      last_active_at: instance.last_active_at,
      updated_at: instance.updated_at,
      terminal_reason: instance.terminal_reason
    }
  end

  # The publisher's current per-node xDS version for this workload's fan-out is
  # not workload-scoped (the version is per NODE, one snapshot carries all
  # workloads), so "generation" here is the max ETS-row generation among the
  # workload's instances: a monotonic-ish marker an operator can watch advance
  # across bank/relight cycles. A best-effort read; absent instances read 0.
  defp serving_generation(workload) do
    serving_store().list(serving_store_server(), workload)
    |> Enum.map(&(&1.generation || 0))
    |> Enum.max(fn -> 0 end)
  end

  # DELETE /v1/serving/:name/instances handler: forced roll (management auth). Drives
  # ServingSweeper.force_roll (drain + destroy every live instance, evict banked
  # snapshots) so the next miss cold-starts on the current base. Returns the counts;
  # a workload with no instances rolls zero (200), never a 404.
  defp handle_force_roll(conn, workload) do
    %{destroyed: destroyed, evicted: evicted} =
      serving_sweeper().force_roll(serving_sweeper_server(), workload)

    send_json(conn, 200, %{workload: workload, destroyed: destroyed, evicted: evicted})
  end

  defp serving_store, do: Application.get_env(:embervm, :serving_store_mod, Embervm.ServingStore)
  defp serving_store_server, do: Application.get_env(:embervm, :serving_store, Embervm.ServingStore)

  # -- stateful handler (R4) -------------------------------------------------

  # GET /v1/stateful/:name (management auth): the singleton stateful workload's
  # current state, its published L4 endpoint (or nil), the banked bundle's stamped
  # generation, whether the bundle pairs with the current volume, and the volume's
  # allocated bytes. Read-only operational introspection behind the same /v1
  # management auth as every other non-session route. A 404 is returned when `name`
  # is not a STATEFUL-class workload in the catalog (an unknown or wrong-class
  # workload is a genuine not-found here, unlike the serving GET which returns an
  # empty list for a defined-but-cold workload; a stateful GET's whole shape is
  # workload-class-specific, so it only answers for a real stateful workload).
  defp handle_get_stateful(conn, workload) do
    if stateful_workload?(workload) do
      instances = stateful_store().list(stateful_store_server(), workload)
      # The instance the top-level fields describe: the live one if present (there
      # is at most one by the singleton invariant), else the banked one, else the
      # newest row (list/2 is newest-created first). This picks the row an operator
      # cares about, not a stale terminal one, when both exist.
      primary = primary_stateful_instance(instances)
      banked = Enum.find(instances, &(&1.state == :banked))
      volume = stateful_store().get_volume(stateful_store_server(), workload)

      send_json(
        conn,
        200,
        json_nullify(%{
          workload: workload,
          instance: primary && stateful_instance_view(primary),
          state: primary && to_string(primary.state),
          generation: primary && primary.generation,
          # The banked bundle's stamped generation (the pair key), null if nothing is banked.
          bundle_generation: banked && banked.snapshot_generation,
          pair_valid: stateful_store().pair_valid?(stateful_store_server(), workload),
          # The volume's actual block usage (the watermark), null if there is no volume row.
          volume_bytes: volume && volume.allocated_bytes,
          published_endpoint: stateful_store().published_endpoint(stateful_store_server(), workload)
        })
      )
    else
      send_json(conn, 404, %{error: "unknown stateful workload", workload: workload, retryable: false})
    end
  end

  # The instance the GET's top-level fields describe: the live one (at most one by
  # the singleton invariant), else the banked one, else the newest row, else nil.
  defp primary_stateful_instance(instances) do
    live_states = [:starting, :serving, :banking, :relighting, :cold_booting]

    Enum.find(instances, &(&1.state in live_states)) ||
      Enum.find(instances, &(&1.state == :banked)) ||
      List.first(instances)
  end

  defp stateful_instance_view(instance) do
    %{
      instance_id: instance.instance_id,
      workload: instance.workload,
      state: to_string(instance.state),
      healthy: instance.healthy,
      node_id: instance.node_id,
      ip: instance.ip,
      port: instance.port,
      generation: instance.generation,
      created_at: instance.created_at,
      last_active_at: instance.last_active_at,
      updated_at: instance.updated_at,
      terminal_reason: instance.terminal_reason
    }
  end

  # Whether `workload` is a STATEFUL-class workload in the catalog. Resolves the
  # catalog module + table from app-env so a request test can inject a fake without
  # the live WorkloadWatcher; production reads the supervised catalog table.
  defp stateful_workload?(workload) do
    mod = Application.get_env(:embervm, :workload_catalog_mod, Embervm.WorkloadCatalog)
    table = Application.get_env(:embervm, :workload_catalog_table, Embervm.WorkloadCatalog.table())

    case mod.fetch(table, workload) do
      {:ok, %{class: "stateful"}} -> true
      _ -> false
    end
  end

  defp stateful_store, do: Application.get_env(:embervm, :stateful_store_mod, Embervm.StatefulStore)
  defp stateful_store_server, do: Application.get_env(:embervm, :stateful_store, Embervm.StatefulStore)

  # DELETE /v1/stateful/:name/instance handler (management auth): destroy the
  # live instance + evict the banked bundle via Embervm.StatefulManager. A
  # workload with no instances destroys/evicts zero (200), never a 404 (mirrors
  # the serving forced-roll's "rolling nothing is still success" shape).
  defp handle_destroy_stateful_instance(conn, workload) do
    %{destroyed: destroyed, evicted: evicted} =
      stateful_manager().destroy_instance(stateful_manager_server(), workload)

    send_json(conn, 200, %{workload: workload, destroyed: destroyed, evicted: evicted})
  end

  # DELETE /v1/stateful/:name/volume handler (management auth): the ONLY
  # destructive data verb. 409 while any non-terminal instance exists (the
  # manager's refusal reason); otherwise 200 with deleted: true.
  defp handle_delete_stateful_volume(conn, workload) do
    case stateful_manager().delete_volume(stateful_manager_server(), workload) do
      {:ok, %{deleted: true}} ->
        send_json(conn, 200, %{workload: workload, deleted: true})

      {:error, :instance_exists} ->
        send_json(conn, 409, %{
          error: "an instance still exists for this workload; destroy it first",
          workload: workload,
          retryable: false
        })

      {:error, {:delete_incomplete, nodes}} ->
        send_json(conn, 500, %{
          error: "volume delete incomplete",
          workload: workload,
          nodes: nodes,
          retryable: true
        })

      {:error, reason} ->
        send_json(conn, 500, %{error: "volume delete failed", reason: inspect(reason), workload: workload, retryable: true})
    end
  end

  # POST /v1/stateful/:name/handover/:target. Every refusal that leaves the
  # anchor untouched is a 4xx with retryable:false unless waiting would actually
  # change the answer: :not_banked clears itself once the sweeper banks the live
  # instance, so it is retryable, while a refused export means the source's
  # bytes are not the authoritative ones and no amount of retrying fixes that.
  defp handle_stateful_handover(conn, workload, target) do
    case stateful_handover().move(workload, target) do
      {:ok, moved} ->
        send_json(conn, 200, %{
          workload: workload,
          from: moved.from,
          to: moved.to,
          generation: moved.generation,
          source_evicted: moved.source_evicted
        })

      {:error, :no_volume} ->
        send_json(conn, 404, %{error: "no volume for this workload", workload: workload, retryable: false})

      {:error, :volume_node_missing} ->
        send_json(conn, 409, %{error: "volume has no anchor node recorded", workload: workload, retryable: false})

      {:error, :already_anchored} ->
        send_json(conn, 409, %{error: "volume is already anchored on that node", workload: workload, target: target, retryable: false})

      {:error, {:not_banked, live}} ->
        send_json(conn, 409, %{
          error: "workload has a live instance; it must be banked before its volume can move",
          workload: workload,
          live: live,
          retryable: true
        })

      {:error, :source_export_refused} ->
        send_json(conn, 409, %{
          error: "source refused to export the volume (unblessed or superseded generation); anchor unchanged",
          workload: workload,
          retryable: false
        })

      {:error, {:node_not_reporting, node}} ->
        send_json(conn, 409, %{error: "node is not reporting", node: node, workload: workload, retryable: true})

      {:error, reason} ->
        send_json(conn, 500, %{error: "handover failed", reason: inspect(reason), workload: workload, retryable: true})
    end
  end

  defp stateful_manager, do: Application.get_env(:embervm, :stateful_manager_mod, Embervm.StatefulManager)
  defp stateful_manager_server, do: Application.get_env(:embervm, :stateful_manager, Embervm.StatefulManager)
  defp stateful_handover, do: Application.get_env(:embervm, :stateful_handover_mod, Embervm.StatefulHandover)

  # GET /v1/groups/:name (management auth): the composite group's instance state,
  # members with health, set id + completeness (the banked set_id, null when nothing
  # is banked), subnet, and published entry endpoint. Mirrors handle_get_stateful/2's
  # primary-instance selection.
  defp handle_get_group(conn, workload) do
    if group_workload?(workload) do
      instances = group_store().list(group_store_server(), workload)
      primary = primary_group_instance(instances)
      banked = Enum.find(instances, &(&1.state == :banked))

      members =
        if primary do
          group_store().members(group_store_server(), primary.instance_id)
          |> Enum.map(&group_member_view/1)
        else
          []
        end

      send_json(
        conn,
        200,
        json_nullify(%{
          workload: workload,
          instance: primary && group_instance_view(primary),
          state: primary && to_string(primary.state),
          members: members,
          # The banked bundle-set handle (the whole-set warmth key), null if nothing
          # is banked; its presence means a complete set is banked (a partial set is
          # eagerly evicted, clearing set_id).
          set_id: (primary && primary.set_id) || (banked && banked.set_id),
          subnet_cidr: primary && primary.subnet_cidr,
          degraded_member: primary && primary.degraded_member,
          published_endpoint: group_store().entry_endpoint(group_store_server(), workload)
        })
      )
    else
      send_json(conn, 404, %{error: "unknown composite workload", workload: workload, retryable: false})
    end
  end

  # The instance the GET's top-level fields describe: the live one (at most one by the
  # group-level singleton invariant), else the banked one, else the newest row.
  defp primary_group_instance(instances) do
    live_states = [:creating, :running, :banking, :relighting, :fresh_booting]

    Enum.find(instances, &(&1.state in live_states)) ||
      Enum.find(instances, &(&1.state == :banked)) ||
      List.first(instances)
  end

  defp group_instance_view(instance) do
    %{
      instance_id: instance.instance_id,
      workload: instance.workload,
      state: to_string(instance.state),
      node_id: instance.node_id,
      subnet_cidr: instance.subnet_cidr,
      entry_member: instance.entry_member,
      entry_port: instance.entry_port,
      listen_port: instance.listen_port,
      set_id: instance.set_id,
      degraded_member: instance.degraded_member,
      entry_ip: instance.entry_ip,
      entry_port_published: instance.entry_port_published,
      created_at: instance.created_at,
      last_active_at: instance.last_active_at,
      updated_at: instance.updated_at,
      terminal_reason: instance.terminal_reason
    }
  end

  defp group_member_view(member) do
    %{
      member_name: member.member_name,
      member_index: member.member_index,
      state: member.state,
      healthy: member.healthy,
      vm_id: member.vm_id,
      ip: member.ip,
      snapshot_ref: member.snapshot_ref
    }
  end

  # Whether `workload` is a COMPOSITE-class workload in the catalog. Resolves the
  # catalog module + table from app-env so a request test can inject a fake.
  defp group_workload?(workload) do
    mod = Application.get_env(:embervm, :workload_catalog_mod, Embervm.WorkloadCatalog)
    table = Application.get_env(:embervm, :workload_catalog_table, Embervm.WorkloadCatalog.table())

    case mod.fetch(table, workload) do
      {:ok, %{class: "composite"}} -> true
      _ -> false
    end
  end

  # DELETE /v1/groups/:name/instance handler: forced roll (management auth). Drives
  # GroupSweeper.force_roll (destroy live members + delete network + evict banked set,
  # keep the definition) so the next connection fresh-boots on the current images.
  # Returns the counts; a workload with no instances rolls zero (200), never a 404
  # (mirrors the serving/stateful forced-roll "rolling nothing is still success").
  defp handle_force_roll_group(conn, workload) do
    %{destroyed: destroyed, evicted: evicted} =
      group_sweeper().force_roll(group_sweeper_server(), workload)

    send_json(conn, 200, %{workload: workload, destroyed: destroyed, evicted: evicted})
  end

  defp group_store, do: Application.get_env(:embervm, :group_store_mod, Embervm.GroupStore)
  defp group_store_server, do: Application.get_env(:embervm, :group_store, Embervm.GroupStore)

  defp group_sweeper, do: Application.get_env(:embervm, :group_sweeper_mod, Embervm.GroupSweeper)
  defp group_sweeper_server, do: Application.get_env(:embervm, :group_sweeper, Embervm.GroupSweeper)

  defp serving_sweeper, do: Application.get_env(:embervm, :serving_sweeper_mod, Embervm.ServingSweeper)
  defp serving_sweeper_server, do: Application.get_env(:embervm, :serving_sweeper, Embervm.ServingSweeper)

  # The activator miss handler (front-end only, no management auth): read the
  # ORIGINAL request (method/path/headers/body verbatim, under the 8 MiB envelope
  # cap), ask the ServingManager to wake the workload (single-flight), and on
  # `{:ok, endpoint}` PROXY this request to the woken VM's ip:port streaming the
  # response back. This is the ONE control-plane touch of a serving request; every
  # subsequent request for a now-live workload reaches the VM node-Envoy-direct.
  # The wake-rate/parked caps are keyed by the workload (activator traffic is
  # anonymous end-user traffic with no bearer principal), passed as the manager's
  # principal so the audit ops attribute to the workload.
  defp handle_activator_miss(conn, workload) do
    # Restore any caller trace (an unfurler/browser rarely carries one, but a
    # traced synthetic probe does), then open the activator ROOT span so the
    # manager's `park`/`placement`/`wake`/`publish` and this `proxy` all nest
    # under one per-miss trace. This is the ONE control-plane touch of a serving
    # request (standing decision 1: the HIT path has NO control-plane span); the
    # gate's off-path proof + wake-latency read this connected miss trace. Same
    # `from_remote_span` idiom as the session invoke; see Embervm.SessionTrace.
    SessionTrace.restore_parent(header_value(conn, "traceparent"))

    Tracer.with_span "embervm.serving.activate",
                     %{attributes: %{"ember.workload" => workload, "ember.principal" => activator_principal(workload)}} do
      do_activator_miss(conn, workload)
    end
  end

  defp do_activator_miss(conn, workload) do
    case read_capped_body(conn) do
      {:ok, body, conn} ->
        req = %{
          method: conn.method,
          path: activator_path(conn),
          headers: activator_headers(conn),
          body: body,
          # Serialize the activate ROOT span so the manager's park/placement/wake/
          # publish child spans (opened across the {:miss} GenServer.call and the
          # async {:wake_done} message, where the OTel process context does not
          # follow) nest under it. nil when tracing is off (CI). See SessionTrace.
          traceparent: SessionTrace.current_traceparent()
        }

        # The activator miss path is PUBLIC (a public serving hostname reaches it), so
        # every error response is GENERIC: a short message + a retryable hint, and
        # nothing internal (no gRPC reason, tap IP/port, shim path, or workload name).
        # The diagnostic detail is logged server-side by ServingManager (workload +
        # reason), where ops read it; a public caller must not see cluster internals.
        case serving_manager().miss(serving_manager_server(), workload, req, activator_principal(workload)) do
          {:ok, endpoint} ->
            proxy_to_serving_vm(conn, endpoint, req)

          {:error, {:wake_rate, _}} ->
            send_json(conn, 429, %{error: "rate limit exceeded", retryable: true})

          {:error, {:park_full, _}} ->
            send_json(conn, 503, %{error: "service temporarily unavailable", retryable: true})

          {:error, {:wake_failed, _reason}} ->
            send_json(conn, 503, %{error: "service temporarily unavailable", retryable: true})

          {:error, {:unknown_workload}} ->
            send_json(conn, 404, %{error: "not found", retryable: false})

          {:error, _reason} ->
            send_json(conn, 503, %{error: "service temporarily unavailable", retryable: true})
        end

      {:error, :too_large} ->
        send_json(conn, 413, %{error: "request body exceeds 8 MiB", retryable: false})
    end
  end

  # Stream the request to the serving VM and the response back. A pre-first-byte
  # proxy error (VM unreachable) becomes a 502; once streaming has begun the
  # ServingProxy owns the (already-committed) connection.
  defp proxy_to_serving_vm(conn, endpoint, req) do
    case Embervm.ServingProxy.proxy(conn, endpoint, req) do
      {:ok, sent_conn} ->
        sent_conn

      {:error, reason} ->
        send_json(conn, 502, %{error: "serving proxy failed", reason: inspect(reason), retryable: true})
    end
  end

  # The guest path the activator proxies to: the ORIGINAL request path (the node
  # Envoy routed the whole request verbatim, so the guest sees its own path), with
  # the query string preserved.
  defp activator_path(conn) do
    case conn.query_string do
      "" -> conn.request_path
      nil -> conn.request_path
      qs -> conn.request_path <> "?" <> qs
    end
  end

  # Every ORIGINAL request header is forwarded to the guest EXCEPT the activator's
  # own routing header (x-ember-workload is control-plane framing, not the guest's).
  # The ServingProxy strips framing/hop-by-hop on top of this.
  defp activator_headers(conn) do
    conn.req_headers
    |> Enum.reject(fn {k, _v} -> String.downcase(k) == @workload_header end)
    |> Map.new(fn {k, v} -> {String.downcase(k), v} end)
  end

  # Activator traffic is anonymous end-user traffic (no bearer principal), so the
  # wake-rate limit + audit ops are keyed by the workload itself: a serving
  # workload can be woken at most wake_max times per window regardless of caller.
  defp activator_principal(workload), do: "serving:#{workload}"

  defp serving_manager, do: Application.get_env(:embervm, :serving_manager_mod, Embervm.ServingManager)
  defp serving_manager_server, do: Application.get_env(:embervm, :serving_manager, Embervm.ServingManager)

  defp session_view(session) do
    %{
      session_id: session.session_id,
      workload: session.workload,
      principal: session.principal,
      state: to_string(session.state),
      generation: session.generation,
      base_digest: session.base_digest,
      created_at: session.created_at,
      last_invoke_at: session.last_invoke_at,
      expires_at: session.expires_at,
      updated_at: session.updated_at,
      terminal_reason: session.terminal_reason
    }
  end

  # Resolvable session modules/servers, symmetric with `store/0`: the concrete
  # module (so a test can pass a fake) and the server name/pid it addresses.
  defp session_manager, do: Application.get_env(:embervm, :session_manager, Embervm.SessionManager)
  defp session_manager_server, do: Application.get_env(:embervm, :session_manager_server, Embervm.SessionManager)
  defp session_store, do: Application.get_env(:embervm, :session_store_mod, Embervm.SessionStore)
  defp session_store_server, do: Application.get_env(:embervm, :session_store, Embervm.SessionStore)

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
    base = %{headers: guest_headers(conn), body_b64: Base.encode64(body)}

    # Only record a guest path when X-Ember-Guest-Path is EXPLICITLY set, so the
    # dispatcher falls back to the workload's `invokePath` (the intended default).
    # Baking "/" here made invokePath dead: the dispatcher prefers the stored
    # request path (`req_env["path"] || invoke_path`), so a "/" default always won
    # and 404'd a guest that only serves /invoke.
    base =
      case header_value(conn, @path_header) do
        nil -> base
        path -> Map.put(base, :path, path)
      end

    # Capture the W3C traceparent (Task 13 distributed tracing): stored in the
    # submitted op so the dispatcher can restore the CALLER's trace context and
    # nest the dispatch/guest_exec spans under it, joining the caller's trace (the
    # demos waterfall). Async submit means the dispatch happens off-request, so
    # the context must ride the durable op-log, not the live process context.
    base =
      case header_value(conn, "traceparent") do
        nil -> base
        tp -> Map.put(base, :traceparent, tp)
      end

    case header_value(conn, "content-type") do
      nil -> base
      ct -> Map.put(base, :content_type, ct)
    end
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

  # Framing / hop-by-hop headers the server MUST own: a guest may not set these,
  # they are the connection's business, not the payload's. Compared lowercased.
  # `x-ember-truncated` is also reserved (set by the caller above, never overwritten).
  @denied_guest_headers MapSet.new([
                          "content-length",
                          "transfer-encoding",
                          "connection",
                          "keep-alive",
                          "upgrade",
                          "host",
                          "te",
                          "trailer",
                          "proxy-authorization",
                          "proxy-authenticate",
                          "x-ember-truncated"
                        ])

  # Replays the guest's response headers onto the caller's connection under a
  # defense-in-depth deny-list (functions are author-vetted, but the control plane
  # still owns framing/hop-by-hop headers). A guest content-type wins; with none set
  # we fall back to application/octet-stream (today's behavior for old/headerless
  # results). Keys are lowercased for Plug and to match the deny-list.
  defp send_guest_result(conn, status_code, body, headers) when is_map(headers) do
    allowed =
      Enum.filter(headers, fn {k, _v} ->
        not MapSet.member?(@denied_guest_headers, String.downcase(to_string(k)))
      end)

    conn =
      Enum.reduce(allowed, conn, fn {k, v}, acc ->
        put_resp_header(acc, String.downcase(to_string(k)), to_string(v))
      end)

    conn =
      if has_header?(allowed, "content-type") do
        conn
      else
        put_resp_content_type(conn, "application/octet-stream")
      end

    send_resp(conn, status_code, body || "")
  end

  defp send_guest_result(conn, status_code, body, _headers) do
    conn
    |> put_resp_content_type("application/octet-stream")
    |> send_resp(status_code, body || "")
  end

  defp has_header?(pairs, name) do
    Enum.any?(pairs, fn {k, _v} -> String.downcase(to_string(k)) == name end)
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

  # OTP's :json.encode renders the Elixir `nil` atom as the JSON STRING "nil" (nil
  # is just an atom to the encoder), not JSON null. Rather than depend on the
  # encoder's atom handling, OMIT nil-valued keys entirely: a JSON object without a
  # key decodes to a map where accessing it yields nil, which is exactly what a
  # genuinely-null field (a banked stateful workload's absent live endpoint, an
  # absent bundle generation) should read back as. Recurses into nested maps/lists.
  defp json_nullify(map) when is_map(map) do
    map
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
    |> Map.new(fn {k, v} -> {k, json_nullify(v)} end)
  end

  defp json_nullify(list) when is_list(list), do: Enum.map(list, &json_nullify/1)
  defp json_nullify(other), do: other

  # The submit API reads/writes only through TaskStore (ETS + result store),
  # never the op-log internals. The store is resolvable from app env for symmetry
  # with the authenticator, defaulting to the supervised singleton.
  defp store, do: Application.get_env(:embervm, :task_store, Embervm.TaskStore)

  defp now_ms, do: System.system_time(:millisecond)
end
