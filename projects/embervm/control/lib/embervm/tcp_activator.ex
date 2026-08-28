defmodule Embervm.TcpActivator do
  @moduledoc """
  The L4 (raw TCP) activator (R4, Task 8): the wake-on-connect front end for
  stateful workloads, the TCP counterpart of the HTTP activator
  `Embervm.ServingManager` fronts. Binds every port in the values-declared
  stateful TCP range (default `5400..5409`, mirroring the chart's
  `servingEnvoy.statefulTcpPortRange`) on the control-plane pod using plain
  `:gen_tcp` (no extra dependency: `ThousandIsland` is a transitive dep of
  `bandit` but is not staged for direct use outside the Bazel-generated
  `deps/`, and a raw accept-loop-per-port is simple enough that pulling it in
  would add risk for no real benefit here).

  ## the activator design (settled; see the moduledoc of `Embervm.StatefulManager`)

  There is no L4 equivalent of an HTTP header to resolve the workload from: the
  node Envoy's stateful `tcp_proxy` cluster fallback for workload W is
  `{activator_ip, W.stateful.listen_port}` (per `Embervm.EndpointPublisher`'s
  stateful render, fixed in this task from a single fixed `activator_tcp_endpoint`
  to a PER-WORKLOAD `{activator_ip, listen_port}` derivation). So the LOCAL PORT
  a connection was accepted on IS the workload identity: this module binds one
  listener per declared stateful port and resolves the workload by matching that
  port against `Embervm.WorkloadCatalog`'s `stateful.listen_port`. A connection
  reaching this module IS the miss signal, by construction (decision 5).

  ## per-port listener, per-connection acceptor process

  One `:gen_tcp` listen socket is opened per port in the range at `init/1`
  (`{:active, false}`, so reads are pulled explicitly, needed for the splice
  loop below); a supervised acceptor process per listener blocks in `:gen_tcp.accept/1`
  and spawns a fresh handler process for each accepted connection, immediately
  looping back to accept the next one. A handler crash (a bad splice) is isolated
  to that one connection; the acceptor loop itself is wrapped so an accept error
  does not kill the listener.

  ## the miss decision + the straggler race

  On accept, `:inet.sockname/1` gives the LOCAL port (identifies the workload).
  If `Embervm.StatefulStore.published_endpoint/1` ALREADY shows a live
  endpoint (a race: the VM came up between the node Envoy's empty-cluster
  fallback decision and this accept), the connection is spliced DIRECTLY to it,
  no wake. Otherwise `Embervm.StatefulManager.wake/3` is called, which
  single-flights: this handler process IS the `from` parked behind the
  GenServer call (`:infinity`, bounded by the manager's reply contract), so
  correctness here reduces entirely to the manager's single-flight guarantee.

  ## the byte splice (bidirectional, half-close propagated, framing-agnostic)

  Once woken, a fresh `:gen_tcp` connection is dialed to the VM's `{ip, port}`
  and TWO processes pump bytes: the handler process reads from the CLIENT
  socket and writes to the UPSTREAM (VM) socket; a spawned peer process reads
  from UPSTREAM and writes to CLIENT. Each direction is a DUMB byte pump (no
  framing assumptions: opaque L4, decision 4) using `{:active, false}` so a
  slow reader cannot let the mailbox unboundedly buffer the other direction's
  backpressure. When either side's read returns `{:error, :closed}` (or any
  other read error), that pump:

    1. calls `:gen_tcp.shutdown(other_socket, :write)` to propagate a HALF-CLOSE
       (a client that sent its request and shut down its write side, "I am
       done sending", must see the VM's write-half close-when-the-VM-is-done
       and vice versa; a full immediate close on first EOF would truncate a
       still-in-flight response/request in the other direction), then
    2. drains until ITS OWN read direction also closes or the peer confirms.

  Both sockets are ALWAYS closed (each pump closes its own read/write pair, and
  the peer processes are linked so a crash cascades to a clean teardown) when the
  connection life-cycle ends, whichever side closes first. This mirrors a plain
  TCP proxy (nginx `stream`, HAProxy `mode tcp`): no request/response framing is
  imposed at any point.

  ## wake failure / timeout

  A `{:error, _}` from `StatefulManager.wake/3` (rate limit, park-full,
  wake-failed, or the volume's anchor node gone) simply closes the accepted
  client socket (a TCP RST-adjacent close, no explicit reset frame at this
  layer: `:gen_tcp.close/1` sends a normal FIN, which is what a raw byte-proxy
  can offer without lower-level socket options). The RATIONALE for why this is
  sufficient: `Embervm.StatefulManager` owns the parked-cap/wake-rate audit ops
  (the observable), and the client's own reconnect (on seeing the connection
  drop) is the retry path -- there is no request-level replay to hold onto at
  this layer, unlike the serving HTTP activator's parked request.

  ## the activator's own advertised endpoint

  `activator_ip` (from `EMBERVM_STATEFUL_ACTIVATOR_IP`, the pod's own routable
  IP via the k8s downward API) is threaded into `Embervm.EndpointPublisher`'s
  `activator_ip` option (this task also fixes that option from a single fixed
  `activator_tcp_endpoint` to this per-workload derivation), so each stateful
  workload's empty-cluster fallback is `{activator_ip, workload.listen_port}`.

  ## test / empty-range mode

  With an EMPTY port range or a `nil` `activator_ip` (the default in a unit
  test of `Embervm.StatefulManager` alone), `init/1` binds NOTHING: no listener,
  no acceptor, a clean no-op process. This lets `StatefulManager` tests run
  without a real socket, exactly like `Embervm.ServingManager` needs no live
  HTTP listener to unit-test the wake brain.
  """

  use GenServer
  require Logger

  # Tracer.with_span/set_attributes are OpenTelemetry.Tracer MACROS, so the module
  # must be required even though it is called fully-qualified via the alias.
  require OpenTelemetry.Tracer, as: Tracer

  # -- Client API --------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  # -- GenServer callbacks -------------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      port_range: Keyword.get(opts, :port_range, []),
      # The stateful manager to call on a stateful miss. Injectable for tests (a fake
      # module/pid implementing wake/3).
      manager: Keyword.get(opts, :manager, Embervm.StatefulManager),
      manager_mod: Keyword.get(opts, :manager_mod, Embervm.StatefulManager),
      catalog_table: Keyword.get(opts, :catalog_table, Embervm.WorkloadCatalog.table()),
      store: Keyword.get(opts, :store, Embervm.StatefulStore),
      store_mod: Keyword.get(opts, :store_mod, Embervm.StatefulStore),
      # The composite-group wake brain to call on a group miss (R5, Task 7): a
      # connection accepted on a composite entry.listenPort resolves the workload by
      # the SAME local-accept-port mechanism as stateful (decision 5), then wakes the
      # GROUP (relight-or-create single-flighted there) instead of the stateful VM.
      # Injectable for tests (a fake module implementing wake/3). group_store_mod is
      # read purely for the straggler check (a group whose entry is already live).
      group_manager: Keyword.get(opts, :group_manager, Embervm.GroupWakeManager),
      group_manager_mod: Keyword.get(opts, :group_manager_mod, Embervm.GroupWakeManager),
      group_store: Keyword.get(opts, :group_store, Embervm.GroupStore),
      group_store_mod: Keyword.get(opts, :group_store_mod, Embervm.GroupStore),
      # The dial seam for the upstream (woken VM) connection, injectable for
      # tests. Production dials plain gen_tcp. Arity-3: (ip, port,
      # connect_timeout_ms) -> {:ok, socket} | {:error, reason}.
      dial_fun: Keyword.get(opts, :dial_fun, &default_dial/3),
      connect_timeout_ms: Keyword.get(opts, :connect_timeout_ms, 5_000),
      # The composite live-splice counter table (R5, Task 8): a composite splice is
      # bracketed incr..decr on it so the GroupSweeper's idle predicate can see a
      # session that began during a wake (invisible to the entry listener's Envoy
      # cx_active counter). nil disables the bracket (a stateful-only or test run),
      # a clean no-op via ActivatorSplices' own absent-table guard.
      splices_table: Keyword.get(opts, :splices_table, Embervm.ActivatorSplices),
      listeners: %{}
    }

    state =
      Enum.reduce(state.port_range, state, fn port, acc ->
        case listen(port) do
          {:ok, lsock} ->
            {:ok, acceptor} =
              Task.start_link(fn -> accept_loop(lsock, port, acc) end)

            Logger.info("embervm tcp activator: listening", port: port)
            %{acc | listeners: Map.put(acc.listeners, port, %{lsock: lsock, acceptor: acceptor})}

          {:error, reason} ->
            Logger.error("embervm tcp activator: failed to bind stateful port", port: port, reason: inspect(reason))
            acc
        end
      end)

    {:ok, state}
  end

  @impl true
  def terminate(_reason, state) do
    for {_port, %{lsock: lsock}} <- state.listeners, do: :gen_tcp.close(lsock)
    :ok
  end

  @impl true
  def handle_info(_msg, state), do: {:noreply, state}

  # -- listen + accept loop ------------------------------------------------------

  defp listen(port), do: listen(port, 10)

  # Bind the listener, retrying briefly on :eaddrinuse. reuseaddr clears a socket
  # lingering in TIME_WAIT, but it cannot bind over a still-open listener: on a
  # control-plane restart the new activator rebinds the same fixed stateful port
  # range the prior instance held, and its socket can take a beat to release. A
  # silent drop here would leave that port's stateful workload permanently
  # unwakeable until the next restart, so we retry the address for ~1s before
  # giving up. (Also deflakes the fixed-port test suite, where serial tests reuse
  # the same ports.)
  defp listen(port, attempts_left) do
    opts = [:binary, packet: :raw, active: false, reuseaddr: true, backlog: 128]

    case :gen_tcp.listen(port, opts) do
      {:error, :eaddrinuse} when attempts_left > 1 ->
        Process.sleep(100)
        listen(port, attempts_left - 1)

      result ->
        result
    end
  end

  # Runs in its own linked Task process (one per bound port): blocks in accept,
  # spawns a handler for the connection, loops. Wrapped so a transient accept
  # error (e.g. :emfile under fd pressure) does not crash the acceptor outright;
  # it logs and retries after a short backoff instead of taking the listener down.
  defp accept_loop(lsock, port, ctx) do
    case :gen_tcp.accept(lsock) do
      {:ok, csock} ->
        {:ok, _pid} =
          Task.start(fn -> handle_connection(csock, port, ctx) end)

        accept_loop(lsock, port, ctx)

      {:error, :closed} ->
        :ok

      {:error, reason} ->
        Logger.warning("embervm tcp activator: accept failed", port: port, reason: inspect(reason))
        Process.sleep(50)
        accept_loop(lsock, port, ctx)
    end
  end

  # -- per-connection handling ----------------------------------------------------

  defp handle_connection(csock, port, ctx) do
    case workload_for_port(ctx, port) do
      nil ->
        Logger.warning("embervm tcp activator: no stateful/composite workload owns port #{port}; closing")
        :gen_tcp.close(csock)

      {class, workload} ->
        route_connection(csock, class, workload, ctx)
    end
  end

  # The straggler check + wake dispatch is CLASS-parameterized: a stateful listener
  # reads StatefulStore + StatefulManager, a composite listener reads GroupStore +
  # GroupWakeManager. Both resolve the workload by the LOCAL ACCEPT PORT (decision 5)
  # and single-flight the wake in their respective manager; the splice below is
  # identical (opaque L4 bytes either way).
  defp route_connection(csock, class, workload, ctx) do
    case live_endpoint(ctx, class, workload) do
      %{ip: ip, port: vm_port} when is_binary(ip) and ip != "" and is_integer(vm_port) ->
        # Straggler: the VM/group is already live (a race with the node Envoy's
        # fallback decision). Splice directly, no wake.
        splice_to(csock, class, workload, ip, vm_port, ctx)

      _ ->
        wake_and_splice(csock, class, workload, ctx)
    end
  rescue
    e ->
      Logger.warning("embervm tcp activator: connection handling raised", workload: workload, error: inspect(e))
      :gen_tcp.close(csock)
  end

  defp live_endpoint(ctx, :stateful, workload) do
    ctx.store_mod.published_endpoint(ctx.store, workload)
  end

  defp live_endpoint(ctx, :composite, workload) do
    ctx.group_store_mod.entry_endpoint(ctx.group_store, workload)
  end

  defp wake_and_splice(csock, class, workload, ctx) do
    {manager, manager_mod, principal} = wake_target(ctx, class, workload)

    case manager_mod.wake(manager, workload, principal) do
      {:ok, %{ip: ip, port: vm_port}} ->
        splice_to(csock, class, workload, ip, vm_port, ctx)

      {:error, reason} ->
        Logger.info("embervm tcp activator: wake denied/failed, closing", workload: workload, reason: inspect(reason))
        :gen_tcp.close(csock)
    end
  end

  defp wake_target(ctx, :stateful, workload), do: {ctx.manager, ctx.manager_mod, "system:stateful:#{workload}"}
  defp wake_target(ctx, :composite, workload), do: {ctx.group_manager, ctx.group_manager_mod, "system:group:#{workload}"}

  defp splice_to(csock, class, workload, ip, vm_port, ctx) do
    case ctx.dial_fun.(ip, vm_port, ctx.connect_timeout_ms) do
      {:ok, usock} ->
        # The `splice` span (Task 10): bounds the whole spliced connection's
        # lifetime (client-to-VM byte pump, both directions) so a stuck or very
        # long-lived stateful connection is visible in telemetry. A ROOT span (a raw
        # TCP accept carries no caller trace to nest under, same shape as the
        # manager's park/wake spans). ember.bytes_in/ember.bytes_out are the
        # lightweight counters pump_both/2 already threads through its
        # accumulator (no extra syscalls: :gen_tcp.recv already returns the byte
        # count via byte_size/1 on data it already read).
        #
        # A COMPOSITE splice is bracketed incr..decr on the live-splice counter (R5,
        # Task 8): a session spliced here never re-enters the entry listener's Envoy
        # cx_active counter, so the GroupSweeper reads this count as its third idle
        # clause. The `after` fires on a normal teardown AND a pump crash within this
        # process (a linked-peer crash cascades here too), so a live splice decrements
        # exactly once; a NET leak only ever reads busier-than-real (never fakes idle).
        with_splice_bracket(class, workload, ctx, fn ->
          # `ember.group` is true for a composite splice (Task 9): the activator spans
          # gain the composite marker so a group entry-member splice is distinguishable
          # from a stateful one in trace views (both share the splice span
          # name, since the byte-pump mechanism is identical).
          Tracer.with_span "embervm.stateful.splice",
                           %{attributes: %{"ember.workload" => workload, "ember.group" => class == :composite}} do
            {bytes_in, bytes_out} = pump_both(csock, usock)
            Tracer.set_attributes(%{"ember.bytes_in" => bytes_in, "ember.bytes_out" => bytes_out})
          end
        end)

      {:error, reason} ->
        Logger.warning("embervm tcp activator: dial to woken VM failed", ip: ip, port: vm_port, reason: inspect(reason))
        :gen_tcp.close(csock)
    end
  end

  # Bracket a COMPOSITE splice with incr..decr on the live-splice counter; a stateful
  # splice runs the body unbracketed (Envoy's state-<port> cx_active already sees a
  # stateful session, so there is nothing extra to track). The decrement is in an
  # `after` so it fires on both a clean teardown and a crash in this handler process.
  defp with_splice_bracket(:composite, workload, ctx, body) do
    Embervm.ActivatorSplices.incr(ctx.splices_table, workload)

    try do
      body.()
    after
      Embervm.ActivatorSplices.decr(ctx.splices_table, workload)
    end
  end

  defp with_splice_bracket(_class, _workload, _ctx, body), do: body.()

  defp default_dial(ip, port, connect_timeout_ms) do
    ip_charlist = String.to_charlist(ip)

    :gen_tcp.connect(ip_charlist, port, [:binary, packet: :raw, active: false], connect_timeout_ms)
  end

  # -- the byte splice -----------------------------------------------------------

  # Two directions, two processes: THIS process pumps client -> upstream; a
  # spawned, LINKED peer pumps upstream -> client. Linking means either pump
  # crashing tears the other down too, so a connection never leaks half-spliced.
  # Both sockets are closed by whichever side detects EOF/error first, with the
  # OTHER socket's write-half shut down first (half-close propagation) so any
  # still-in-flight bytes already queued in the other direction are not
  # truncated by an immediate full close.
  #
  # Returns `{bytes_in, bytes_out}` (client->upstream, upstream->client) for the
  # `splice` span's byte counters (Task 10): each direction's pump/2 already
  # accumulates the count it read, so this costs no extra syscall.
  defp pump_both(csock, usock) do
    parent = self()

    {:ok, peer} =
      Task.start_link(fn ->
        bytes_out = pump(usock, csock, 0)
        send(parent, {:peer_done, self(), bytes_out})
      end)

    bytes_in = pump(csock, usock, 0)

    # Wait briefly for the peer to notice the shutdown and finish its own
    # drain; it is linked so a crash here would already have propagated, this
    # is just to avoid leaking the peer task if it is still draining.
    bytes_out =
      receive do
        {:peer_done, ^peer, n} -> n
      after
        5_000 -> 0
      end

    :gen_tcp.close(csock)
    :gen_tcp.close(usock)

    {bytes_in, bytes_out}
  end

  # A dumb byte pump: read from `from_sock`, write to `to_sock`, repeat, until
  # `from_sock` errors/closes. {:active, false} + recv with a bounded size keeps
  # this a pull-based loop (no unbounded mailbox growth from an eager reader).
  # On EOF/error, shuts down `to_sock`'s WRITE half (half-close propagation: the
  # peer sees "no more bytes coming this way" without losing its own still-open
  # read direction) rather than a hard close, which the caller (pump_both) does
  # once both directions have finished. `acc` is the running byte count,
  # returned when the direction closes (the splice span's ember.bytes_in/out).
  defp pump(from_sock, to_sock, acc) do
    case :gen_tcp.recv(from_sock, 0, :infinity) do
      {:ok, data} ->
        case :gen_tcp.send(to_sock, data) do
          :ok ->
            pump(from_sock, to_sock, acc + byte_size(data))

          {:error, _reason} ->
            safe_shutdown(from_sock, :read)
            acc
        end

      {:error, :closed} ->
        safe_shutdown(to_sock, :write)
        acc

      {:error, _reason} ->
        safe_shutdown(to_sock, :write)
        acc
    end
  end

  defp safe_shutdown(sock, how) do
    :gen_tcp.shutdown(sock, how)
  rescue
    _ -> :ok
  catch
    _, _ -> :ok
  end

  # -- workload resolution ---------------------------------------------------

  # Resolve the workload (and its CLASS) that owns the local accept `port`: a
  # stateful workload by its `stateful.listen_port`, or a composite group by its
  # `group.entry.listen_port` (the R4 stateful range 5400-5409 and the R5 composite
  # range 5410-5419 are disjoint, and each workload's listen port is unique across
  # both classes by the WorkloadWatcher's validation, so at most one match). Returns
  # `{:stateful, name}` | `{:composite, name}` | nil.
  defp workload_for_port(ctx, port) do
    Embervm.WorkloadCatalog.all_names(ctx.catalog_table)
    |> Enum.find_value(fn name ->
      case Embervm.WorkloadCatalog.fetch(ctx.catalog_table, name) do
        {:ok, %{class: "stateful", stateful: %{listen_port: ^port}}} -> {:stateful, name}
        {:ok, %{class: "composite", group: %{entry: %{listen_port: ^port}}}} -> {:composite, name}
        _ -> false
      end
    end)
  end
end
