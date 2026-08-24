defmodule Embervm.Session do
  @moduledoc """
  One supervised GenServer per LIVE session (under `Embervm.SessionSupervisor`, a
  DynamicSupervisor), owning that session's invoke serialization and all daemon
  calls for its VM. A BANKED session has NO process: its durable row is enough,
  and PR-4's relight-on-invoke restarts a process from the row on the next hit.

  ## one in-flight invoke, FIFO queue (standing decision 9)

  Invokes to one session serialize FIFO through this process: an agent turn is
  sequential by nature, so at most one `SessionAssign` is ever in flight for a
  session. This process owns:

    * a bounded FIFO queue of waiting callers (cap `invoke_queue_cap`, default 4);
      a pile-up past the cap is rejected `{:error, :queue_full}` (the router 429s),
      the session-scoped analog of the dispatcher's per-principal queue-depth cap;
    * the in-flight invoke: exactly one caller's `SessionAssign` runs at a time,
      in a `spawn_monitor` worker so the (up to `timeout_ms`) guest round-trip
      never blocks this process from accepting/queuing the next caller or handling
      a lifecycle message.

  The caller blocks in `invoke/3` on a `GenServer.call` with `:infinity` timeout:
  the router's own request process IS the parked BEAM process the spec wants, and
  the reply is sent from the worker-completion handler once its turn runs. The
  queue-cap check happens synchronously in the call handler BEFORE parking, so an
  over-cap caller gets its 429 immediately rather than parking then timing out.

  ## the invoke is a SessionAssign (deliver-without-destroy)

  Unlike a task's `Assign` (which destroys the VM), a session invoke is a
  `SessionAssign`: the guest handles the request and the VM SURVIVES for the next
  invoke, accreting state. The guest response (status, headers, body) is returned
  VERBATIM to the parked caller, carrying the guest's own headers (the R1 sync-wait
  header carry). Usage rides the `session_invoked` op (D12.1), so session compute
  is quota-visible exactly like task compute.

  ## failure posture

  A `DEADLINE_EXCEEDED` or transport error on `SessionAssign` (or a `suspect` VM
  the daemon left alive but flagged) marks the session `failed` and destroys the
  VM: a guest in an unknown mid-request state must not accrete further state
  silently. The parked caller gets the error; every still-queued caller gets a
  `{:error, :failed}` (the router 410s them). This process then stops.

  The wall-clock watchdog (#4434) is deliberately gentler, because it fires in
  the opposite diagnostic situation: the server never enforced its deadline
  because the exchange never completed through the channel at all (an orphaned
  channel, #4419), so a fired watchdog is evidence about the CLIENT side of the
  stream, not about the guest. The wedged worker is killed (its stream dies with
  its process) and the timed-out caller gets `{:error, :invoke_timeout}`, but
  the session stays live: queued turns dispatch normally, quiescence re-arms
  the idle timer, and the session banks, releasing its workload-cap slot
  without discarding guest state.

  ## the idle-bank timer (Task 7)

  A live session with ZERO in-flight and ZERO queued invokes for `idle_bank_ms`
  banks: releasing its live VM while retaining state on a node-disk snapshot. This
  process owns ONLY the quiescence detection and the timer; when it fires on a
  genuinely idle session it ASKS the manager to bank (`SessionManager.bank/2`) and,
  if the manager ADMITS the bank (`:ok`), STOPS immediately, handing the whole bank
  lifecycle (the RPC, the durable `session_banked` append, and failure recovery) to
  the manager, which runs the RPC OFF its own process so a bank never head-of-line-
  blocks other sessions' routing (gate 3). A banked session has no process.

  If the manager REFUSES the bank (another bank in flight on the node, the disk
  fail-closed gate, or the session already moved off running), this process re-arms
  the timer and stays live. The timer is armed whenever the process goes quiescent
  and cancelled whenever work arrives, so it only ever fires on a genuinely idle
  session; an idle-bank that races an invoke re-checks quiescence and re-arms rather
  than banks. `idle_bank_ms` nil/0 disables banking entirely (the session never
  idle-banks; only expiry/destroy/relight-cycle ends it).

  All timers use `Process.send_after` with an injectable `idle_bank_ms`, and tests
  drive `:maybe_bank` directly for determinism.
  """

  use GenServer, restart: :transient
  require Logger
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.Node.V1.{GuestRequest, GuestResponse, SessionAssignRequest, SessionAssignResponse, Trace}
  alias Embervm.SessionTrace

  # Default margin the invoke watchdog adds on top of transport_timeout/1. Matches
  # the dispatcher's @assign_watchdog_margin_ms; overridable per deploy through
  # EMBERVM_SESSION_INVOKE_WATCHDOG_MARGIN_MS.
  @invoke_watchdog_margin_ms 15_000

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Runs one invoke against the session's live VM, parking the caller FIFO behind
  any in-flight/queued invoke. `req` is `%{method, path, headers, body}` (the
  task-envelope allow-list already applied by the router). Returns
  `{:ok, %{status_code, headers, body, usage}}` with the guest response verbatim,
  `{:error, :queue_full}` (429) past the queue cap, or `{:error, reason}` on a
  daemon failure (the session is then `failed`). `:infinity` GenServer timeout:
  the router's request process is the parked waiter, bounded by the guest
  `timeout_ms` per invoke, not by a fixed call timeout.
  """
  @spec invoke(GenServer.server(), map(), timeout()) :: {:ok, map()} | {:error, term()}
  def invoke(server, req, _timeout \\ :infinity) do
    GenServer.call(server, {:invoke, req}, :infinity)
  end

  @doc "The session id this process serves (for supervision/debug)."
  @spec session_id(GenServer.server()) :: String.t()
  def session_id(server), do: GenServer.call(server, :session_id)

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      session_id: Keyword.fetch!(opts, :session_id),
      workload: Keyword.fetch!(opts, :workload),
      principal: Keyword.get(opts, :principal),
      node_id: Keyword.fetch!(opts, :node_id),
      # The channel key this session's per-invoke SessionAssign / destroy dial. Under
      # brick co-location several noded instances share node_id, and the node-name
      # alias collapses to an arbitrary sibling; dial_id is the SPECIFIC instance
      # running this session's VM (instance-key unification PR-B0b). Defaults to
      # node_id so a legacy/single-instance caller (and older tests) is unaffected.
      dial_id: Keyword.get(opts, :dial_id) || Keyword.fetch!(opts, :node_id),
      vm_id: Keyword.fetch!(opts, :vm_id),
      # Session config from the catalog entry: the invoke queue cap, the guest
      # round-trip timeout, and (PR-4) the idle-bank delay.
      queue_cap: Keyword.fetch!(opts, :queue_cap),
      timeout_ms: Keyword.get(opts, :timeout_ms, 90_000),
      # Invoke wall-clock watchdog (#4434). The gRPC deadline on SessionAssign is
      # a header the SERVER enforces, so on an orphaned channel (#4419) nothing
      # fires and the worker sits in build_stream/2 forever, pinning `worker` and
      # the session's slot. The watchdog budget is transport_timeout(timeout_ms)
      # plus this margin, so the server-enforced deadline always gets first shot
      # and the client-side kill is genuinely last resort. Chart-wired via
      # EMBERVM_SESSION_INVOKE_WATCHDOG_MARGIN_MS (values session.invokeWatchdogMarginMs).
      invoke_watchdog_margin_ms:
        Keyword.get(opts, :invoke_watchdog_margin_ms, @invoke_watchdog_margin_ms),
      # Test-only seam: when set, IS the watchdog budget (tests avoid waiting out
      # the 5s transport headroom floor). nil in production.
      invoke_watchdog_ms: Keyword.get(opts, :invoke_watchdog_ms, nil),
      # The guest path a bare invoke (no explicit X-Ember-Guest-Path) is forwarded
      # to. Defaults to the shim's only route, /invoke; the create/restart paths pass
      # the workload's configured invokePath so a custom path is honored.
      invoke_path: Keyword.get(opts, :invoke_path, "/invoke"),
      idle_bank_ms: Keyword.get(opts, :idle_bank_ms, nil),
      session_store: Keyword.get(opts, :session_store, Embervm.SessionStore),
      # The SessionManager this process calls back to for a bank (node-global policy
      # lives there). Defaults to the supervised singleton; the manager injects itself.
      manager: Keyword.get(opts, :manager, Embervm.SessionManager),
      bank_fun: Keyword.get(opts, :bank_fun, &default_bank_call/2),
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      invalidate_fun: Keyword.get(opts, :invalidate_fun, &Embervm.NodeChannel.invalidate/2),
      assign_fun: Keyword.get(opts, :assign_fun, &default_session_assign/2),
      destroy_fun: Keyword.get(opts, :destroy_fun, &default_destroy/2),
      rejoin_failure_fun: Keyword.get(opts, :rejoin_failure_fun),
      # FIFO of {from, req} waiting their turn; the head runs when no worker is in
      # flight. `worker` is the {pid, ref, from, watchdog_timer} of the in-flight
      # invoke, or nil.
      queue: :queue.new(),
      worker: nil,
      # The armed idle-bank timer ref (nil when disarmed).
      idle_timer: nil
    }

    {:ok, arm_idle_timer(state)}
  end

  @impl true
  def handle_call(:session_id, _from, state), do: {:reply, state.session_id, state}

  def handle_call({:invoke, req}, from, state) do
    # Queue-cap is over WAITERS: the in-flight invoke does not count against the
    # cap (it already left the queue), so `invoke_queue_cap` bounds how many callers
    # may PILE UP behind the running one. A cap of 4 admits 4 waiters + 1 in flight.
    if :queue.len(state.queue) >= state.queue_cap do
      {:reply, {:error, :queue_full}, state}
    else
      # Work arrived: disarm the idle timer so a bank cannot start while a caller is
      # queued or in flight (the bank re-arms once the session goes quiescent again).
      state = state |> disarm_idle_timer() |> enqueue(from, req)
      {:noreply, maybe_start_next(state)}
    end
  end

  defp enqueue(state, from, req) do
    # Stamp the enqueue time (native units) so the worker can emit a `queue_wait`
    # span covering park -> dispatch (Task 9: the FIFO wait is a latency phase the
    # Task 11 gates must read from spans).
    %{state | queue: :queue.in({from, req, :opentelemetry.timestamp()}, state.queue)}
  end

  @impl true
  def handle_info({:invoke_done, pid, outcome}, %{worker: {pid, ref, from, timer}} = state) do
    Process.demonitor(ref, [:flush])
    # Defensive cancellation: a late {:invoke_timeout, ref} is harmless because
    # the handler compares the ref against the live worker.
    _ = Process.cancel_timer(timer)
    state = %{state | worker: nil}

    case outcome do
      {:ok, result, usage} ->
        # Record the invoke durably (usage rides the op, D12.1) BEFORE replying, so
        # the caller never sees a response the op-log has not accounted. A store
        # error does not fail the caller's response (the guest already did the work);
        # it is logged and the response still goes back.
        _ = record_invoke(state, usage)
        GenServer.reply(from, {:ok, result})
        {:noreply, maybe_start_next(disarm_rejoin_failure(state))}

      {:error, reason} ->
        # A daemon transport/timeout/suspect failure: the session is failed and its
        # VM destroyed. Mirror the success branch: append session_failed BEFORE
        # replying (#4644, durable-before-observed), so a caller told {:error, _}
        # can read :failed straight back. Then reply, destroy the VM best-effort
        # off the caller's critical path, drain the rest as :failed, and stop (a
        # failed session has no process).
        fail_and_stop(state, reason, from)
    end
  end

  # A worker died without reporting (it always sends {:invoke_done, ...}): treat as
  # a transport failure, same as a reported error (same durable-before-reply order).
  def handle_info({:DOWN, ref, :process, pid, down_reason}, %{worker: {pid, ref, from, timer}} = state) do
    _ = Process.cancel_timer(timer)
    state = %{state | worker: nil}
    fail_and_stop(state, {:worker_down, down_reason}, from)
  end

  # The invoke wall-clock watchdog fired (#4434): the worker has outlived the
  # server-enforced gRPC deadline plus margin, so neither completion path
  # ({:invoke_done, ...} or DOWN) is ever coming: the stream is wedged
  # client-side, which is exactly the case where the server never sees the
  # deadline header (orphaned channel, #4419). Kill the worker (the wedged
  # stream dies with its process) and answer the parked caller, but do NOT fail
  # the session: a reported DEADLINE_EXCEEDED means the guest hung mid-request,
  # while a fired watchdog says nothing reliable about the guest, and the issue's
  # acceptance is that the session survive to serve its queued turns and bank.
  # The queue drains normally through maybe_start_next, quiescence re-arms the
  # idle timer, and the bank releases the workload-cap slot. The rejoin watcher
  # disarms like the success branch: it exists to catch a restored volume that
  # fails delivery, and a client-side wedge does not implicate the volume; a
  # genuinely broken VM surfaces as an ordinary transport failure on the next
  # turn. Keyed on the unique monitor ref (the SessionManager create_timeout
  # pattern), so a stale timer for a finished worker, or a reused pid, never
  # kills the wrong thing.
  def handle_info({:invoke_timeout, ref}, %{worker: {pid, ref, from, _timer}} = state) do
    Process.demonitor(ref, [:flush])

    Logger.warning(
      "session invoke worker watchdog fired (budget=#{invoke_watchdog_ms(state)}ms)",
      session_id: state.session_id,
      workload: state.workload,
      node_id: state.node_id
    )

    Process.exit(pid, :kill)
    state = %{state | worker: nil}
    GenServer.reply(from, {:error, :invoke_timeout})
    {:noreply, maybe_start_next(disarm_rejoin_failure(state))}
  end

  def handle_info({:invoke_timeout, _stale_ref}, state), do: {:noreply, state}

  # The idle-bank timer fired. ASK the manager to bank ONLY if still quiescent (no
  # worker, empty queue); a stale timer (idle_timer already cleared) is ignored. On
  # admission (:ok) the manager owns the bank from here, so this process STOPS. On a
  # refusal it re-arms and stays live.
  def handle_info(:maybe_bank, %{idle_timer: nil} = state), do: {:noreply, state}

  def handle_info(:maybe_bank, state) do
    state = %{state | idle_timer: nil}

    if quiescent?(state) do
      case ask_bank(state) do
        :ok ->
          # Admitted: the manager now owns the whole bank lifecycle. A banked session
          # has no process, so stop. If the manager's async bank later FAILS, the
          # manager restarts a session process from the durable row (adoption/relight),
          # so nothing is lost by stopping here.
          {:stop, :normal, state}

        {:error, _reason} ->
          {:noreply, arm_idle_timer(state)}
      end
    else
      # Raced an invoke: re-arm and try again once idle.
      {:noreply, arm_idle_timer(state)}
    end
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- idle-bank -------------------------------------------------------------

  defp quiescent?(state), do: is_nil(state.worker) and :queue.is_empty(state.queue)

  # Ask the manager to admit a bank. The manager owns the per-node bank cap, the disk
  # fail-closed gate, the Bank RPC (run off its process), the durable session_banked
  # append, and failure recovery. A raise/exit is treated as a refusal (re-arm).
  defp ask_bank(state) do
    state.bank_fun.(state.manager, state.session_id)
  rescue
    _ -> {:error, :bank_call_raised}
  catch
    _, _ -> {:error, :bank_call_raised}
  end

  # Arm the idle-bank timer if banking is enabled and it is not already armed. A nil
  # or non-positive idle_bank_ms disables banking (the timer is never scheduled).
  defp arm_idle_timer(%{idle_bank_ms: ms} = state) when is_integer(ms) and ms > 0 do
    if state.idle_timer do
      state
    else
      %{state | idle_timer: Process.send_after(self(), :maybe_bank, ms)}
    end
  end

  defp arm_idle_timer(state), do: state

  defp disarm_idle_timer(%{idle_timer: nil} = state), do: state

  defp disarm_idle_timer(%{idle_timer: ref} = state) do
    _ = Process.cancel_timer(ref)
    %{state | idle_timer: nil}
  end

  defp default_bank_call(manager, session_id) do
    Embervm.SessionManager.bank(manager, session_id)
  end

  # -- invoke dispatch -------------------------------------------------------

  # Start the head of the queue if nothing is in flight; otherwise leave it queued.
  # When the queue empties with no worker, the session is quiescent: arm the idle
  # timer so a sustained idle period banks.
  defp maybe_start_next(%{worker: nil} = state) do
    case :queue.out(state.queue) do
      {{:value, {from, req, enqueued_at}}, rest} ->
        {pid, ref} = spawn_invoke_worker(state, req, enqueued_at)
        # Last-resort wall clock (#4434): must fire AFTER the gRPC deadline the
        # server enforces, so the normal DEADLINE_EXCEEDED path gets first shot.
        timer = Process.send_after(self(), {:invoke_timeout, ref}, invoke_watchdog_ms(state))
        %{state | queue: rest, worker: {pid, ref, from, timer}}

      {:empty, _} ->
        arm_idle_timer(state)
    end
  end

  defp maybe_start_next(state), do: state

  # The watchdog budget: the injected test value, or the transport deadline (which
  # already exceeds the guest's timeout_ms by @headroom_ms) plus the margin. Same
  # shape as the dispatcher's assign watchdog; the two must stay strictly above
  # their respective server-enforced deadlines.
  defp invoke_watchdog_ms(state) do
    state.invoke_watchdog_ms || transport_timeout(state.timeout_ms) + state.invoke_watchdog_margin_ms
  end

  defp spawn_invoke_worker(state, req, enqueued_at) do
    owner = self()
    node_id = state.node_id
    dial_id = state.dial_id
    vm_id = state.vm_id
    session_id = state.session_id
    workload = state.workload
    principal = state.principal
    timeout_ms = state.timeout_ms
    invoke_path = state.invoke_path
    channel_fun = state.channel_fun
    invalidate_fun = state.invalidate_fun
    assign_fun = state.assign_fun

    spawn_monitor(fn ->
      # Restore the invoke ROOT span (carried on the req from the router) as the
      # remote parent so this worker's phase spans nest under the caller's trace.
      SessionTrace.restore_parent(Map.get(req, :traceparent))

      # queue_wait: park -> dispatch. Emitted with an explicit start_time = enqueue
      # so the span reflects the real FIFO wait (this worker only starts once the
      # session is free), ending now as the guest_exec begins.
      Tracer.with_span "embervm.session.queue_wait",
                       %{
                         start_time: enqueued_at,
                         attributes: %{
                           "ember.session_id" => session_id,
                           "ember.workload" => workload,
                           "ember.principal" => principal
                         }
                       } do
        :ok
      end

      outcome =
        Tracer.with_span "embervm.session.guest_exec",
                         %{
                           attributes: %{
                             "ember.session_id" => session_id,
                             "ember.workload" => workload,
                             "ember.principal" => principal
                           }
                         } do
          run_invoke(%{
            node_id: node_id,
            dial_id: dial_id,
            vm_id: vm_id,
            session_id: session_id,
            workload: workload,
            timeout_ms: timeout_ms,
            invoke_path: invoke_path,
            req: req,
            channel_fun: channel_fun,
            invalidate_fun: invalidate_fun,
            assign_fun: assign_fun
          })
        end

      send(owner, {:invoke_done, self(), outcome})
    end)
  end

  # The worker body (off this GenServer): acquire the shared channel, SessionAssign,
  # and classify the result. A clean guest response (even a 4xx/5xx) is `{:ok, ...}`:
  # unlike a task, a session invoke's guest error is the guest's answer, not a VM
  # failure, so it does NOT fail the session (the caller decides). Only a transport
  # error, a DEADLINE_EXCEEDED, or a daemon-flagged `suspect` VM fails the session.
  defp run_invoke(ctx) do
    case ctx.channel_fun.(ctx.dial_id) do
      {:ok, channel} ->
        guest_req = %GuestRequest{
          method: Map.get(ctx.req, :method, "POST"),
          # Default the guest path to the workload's invokePath (the shim serves
          # ONLY /invoke; a "/" default 404s the guest, the R1 baked-path trap). An
          # EXPLICIT X-Ember-Guest-Path (req.path non-nil) still overrides.
          path: Map.get(ctx.req, :path) || ctx.invoke_path,
          headers: Map.get(ctx.req, :headers, %{}),
          body: Map.get(ctx.req, :body, "")
        }

        assign_req = %SessionAssignRequest{
          trace: %Trace{workload: ctx.workload},
          vm_id: ctx.vm_id,
          request: guest_req,
          timeout_ms: ctx.timeout_ms,
          session_id: ctx.session_id
        }

        case ctx.assign_fun.(channel, assign_req) do
          {:ok, %SessionAssignResponse{suspect: true}} ->
            _ = ctx.invalidate_fun.(ctx.dial_id, channel)
            {:error, :suspect}

          {:ok, %SessionAssignResponse{response: %GuestResponse{} = resp, usage: usage}} ->
            {:ok,
             %{
               status_code: resp.status_code,
               headers: resp.headers || %{},
               body: resp.body || ""
             }, Embervm.Usage.from_proto(usage)}

          {:error, reason} ->
            maybe_invalidate(ctx, channel, reason)
            {:error, classify_error(reason)}
        end

      {:error, reason} ->
        {:error, {:no_channel, reason}}
    end
  end

  # Only a TRANSPORT fault means the shared node channel is bad and must be torn
  # down. A real server-returned gRPC status rode a HEALTHY channel to get here, so
  # invalidating it would needlessly disconnect every other session sharing that
  # channel (D-R2.7.2). BUT the Mint adapter WRAPS a transport death (a replaced noded
  # pod's broken connection) as a %GRPC.RPCError{status: 2} "...the connection is
  # closed": treating that as a healthy-channel status is what wedged the node on a
  # noded pod restart, so invalidate when transport_dead?/1 recognises the wrapped
  # shape. Any non-RPCError reason (raw transport error, closed socket) stays
  # invalidate-always, unchanged.
  defp maybe_invalidate(ctx, channel, %GRPC.RPCError{} = reason) do
    if Embervm.NodeChannel.transport_dead?(reason) do
      _ = ctx.invalidate_fun.(ctx.dial_id, channel)
    end

    :ok
  end

  defp maybe_invalidate(ctx, channel, _reason) do
    _ = ctx.invalidate_fun.(ctx.dial_id, channel)
    :ok
  end

  defp classify_error(%GRPC.RPCError{status: 4}), do: :deadline_exceeded
  defp classify_error(%GRPC.RPCError{} = e), do: {:rpc, e.status}
  defp classify_error(reason), do: reason

  # -- session-store side effects --------------------------------------------

  # Record a successful invoke: append session_invoked with usage (no bodies), which
  # upserts the usage projection in the same transaction (D12.1). Best-effort against
  # the store; a store hiccup does not fail the already-served caller.
  defp record_invoke(state, usage) do
    Embervm.SessionStore.record_invoke(state.session_store, state.session_id, usage)
  rescue
    _ -> :error
  catch
    _, _ -> :error
  end

  # Fail the session: append session_failed, reply {:error, reason} to the in-flight
  # caller, destroy the VM, drain queued callers as :failed, and stop this process.
  # Called on any daemon-level invoke failure.
  #
  # Ordering (#4644): the transition is awaited BEFORE the reply so the caller never
  # observes a failure the op-log has not accounted (invariant 7, the same order
  # record_invoke/reply uses on success). destroy_vm runs AFTER the reply: it is a
  # gRPC call to a daemon that just failed, so it must not sit on the caller's error
  # path, and its result was always discarded anyway (a failed destroy already left
  # "row :failed, VM alive", which the orphan sweep reconciles, invariant 5).
  #
  # The armed rejoin_failure_fun branch is deliberately NOT reordered: it does not
  # fail the session, it hands the outcome to the manager by message (which parks
  # the session, see SessionManager handle_info({:rejoin_assign_failed, ...})), and
  # a synchronous call back into the manager from this process could deadlock
  # against a manager that is itself calling this session. The park is therefore
  # observed-before-durable by design, and nothing reads :failed from it.
  defp fail_and_stop(state, reason, from) do
    if is_function(state.rejoin_failure_fun, 1) do
      failure_fun = state.rejoin_failure_fun
      state = disarm_rejoin_failure(state)
      GenServer.reply(from, {:error, reason})
      _ = failure_fun.(reason)
      drain_queue_as_failed(state)
      {:stop, :normal, %{state | queue: :queue.new()}}
    else
      _ =
        Embervm.SessionStore.transition(
          state.session_store,
          state.session_id,
          :fail,
          :session_failed,
          %{reason: :failed, detail: inspect(reason)},
          %{}
        )

      GenServer.reply(from, {:error, reason})
      _ = destroy_vm(state)
      drain_queue_as_failed(state)
      {:stop, :normal, %{state | queue: :queue.new()}}
    end
  end

  defp disarm_rejoin_failure(state), do: %{state | rejoin_failure_fun: nil}

  defp drain_queue_as_failed(state) do
    for {from, _req, _enqueued_at} <- :queue.to_list(state.queue) do
      GenServer.reply(from, {:error, :failed})
    end
  end

  defp destroy_vm(state) do
    case state.channel_fun.(state.dial_id) do
      {:ok, channel} ->
        try do
          state.destroy_fun.(channel, state.vm_id)
        rescue
          _ -> :error
        catch
          _, _ -> :error
        end

      _ ->
        :error
    end
  end

  # -- defaults --------------------------------------------------------------

  defp default_session_assign(channel, %SessionAssignRequest{timeout_ms: timeout_ms} = req) do
    Embervm.Node.V1.NodeService.Stub.session_assign(channel, req, timeout: transport_timeout(timeout_ms))
  end

  defp default_destroy(channel, vm_id) do
    # Failure teardown runs from this session GenServer's completion handler.
    Embervm.Node.V1.NodeService.Stub.destroy(channel, %Embervm.Node.V1.DestroyRequest{vm_id: vm_id},
      timeout: 15_000
    )
  end

  # Transport timeout must exceed the application deadline (timeout_ms) the guest is
  # told it has, plus headroom so the guest-side timeout fires first and noded can
  # respond with a structured deadline-exceeded error before the transport deadline
  # hits. Without this, the caller gets {:server_closed_request, :cancel} (grpc-elixir
  # 1.0.2 cancel-frame bug #4144) instead of noded's real status.
  # Headroom is 5s: conservative, as a well-behaved guest should not approach its own
  # timeout, and noded's error response is O(1ms). The misnamed default gRPC timeout
  # is 10s; our guest default is 90s, so an explicit transport timeout is needed.
  # MUST stay in sync with dispatcher.ex transport_timeout/1 (see there for motivation).
  @default_timeout_ms 90_000
  @headroom_ms 5_000
  @doc false
  def transport_timeout(nil), do: @default_timeout_ms + @headroom_ms
  def transport_timeout(0), do: @default_timeout_ms + @headroom_ms
  def transport_timeout(timeout_ms) when is_integer(timeout_ms) and timeout_ms > 0 do
    timeout_ms + @headroom_ms
  end
end
