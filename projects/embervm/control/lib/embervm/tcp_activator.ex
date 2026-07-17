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
      # The stateful manager to call on a miss. Injectable for tests (a fake
      # module/pid implementing wake/3).
      manager: Keyword.get(opts, :manager, Embervm.StatefulManager),
      manager_mod: Keyword.get(opts, :manager_mod, Embervm.StatefulManager),
      catalog_table: Keyword.get(opts, :catalog_table, Embervm.WorkloadCatalog.table()),
      store: Keyword.get(opts, :store, Embervm.StatefulStore),
      store_mod: Keyword.get(opts, :store_mod, Embervm.StatefulStore),
      # The dial seam for the upstream (woken VM) connection, injectable for
      # tests. Production dials plain gen_tcp. Arity-3: (ip, port,
      # connect_timeout_ms) -> {:ok, socket} | {:error, reason}.
      dial_fun: Keyword.get(opts, :dial_fun, &default_dial/3),
      connect_timeout_ms: Keyword.get(opts, :connect_timeout_ms, 5_000),
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

  defp listen(port) do
    :gen_tcp.listen(port, [
      :binary,
      packet: :raw,
      active: false,
      reuseaddr: true,
      backlog: 128
    ])
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
        Logger.warning("embervm tcp activator: no stateful workload owns port #{port}; closing")
        :gen_tcp.close(csock)

      workload ->
        route_connection(csock, workload, ctx)
    end
  end

  defp route_connection(csock, workload, ctx) do
    case ctx.store_mod.published_endpoint(ctx.store, workload) do
      %{ip: ip, port: vm_port} when is_binary(ip) and ip != "" and is_integer(vm_port) ->
        # Straggler: the VM is already live (a race with the node Envoy's
        # fallback decision). Splice directly, no wake.
        splice_to(csock, ip, vm_port, ctx)

      _ ->
        wake_and_splice(csock, workload, ctx)
    end
  rescue
    e ->
      Logger.warning("embervm tcp activator: connection handling raised", workload: workload, error: inspect(e))
      :gen_tcp.close(csock)
  end

  defp wake_and_splice(csock, workload, ctx) do
    principal = "system:stateful:#{workload}"

    case ctx.manager_mod.wake(ctx.manager, workload, principal) do
      {:ok, %{ip: ip, port: vm_port}} ->
        splice_to(csock, ip, vm_port, ctx)

      {:error, reason} ->
        Logger.info("embervm tcp activator: wake denied/failed, closing", workload: workload, reason: inspect(reason))
        :gen_tcp.close(csock)
    end
  end

  defp splice_to(csock, ip, vm_port, ctx) do
    case ctx.dial_fun.(ip, vm_port, ctx.connect_timeout_ms) do
      {:ok, usock} ->
        pump_both(csock, usock)

      {:error, reason} ->
        Logger.warning("embervm tcp activator: dial to woken VM failed", ip: ip, port: vm_port, reason: inspect(reason))
        :gen_tcp.close(csock)
    end
  end

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
  defp pump_both(csock, usock) do
    parent = self()

    {:ok, peer} =
      Task.start_link(fn ->
        pump(usock, csock)
        send(parent, {:peer_done, self()})
      end)

    pump(csock, usock)

    # Wait briefly for the peer to notice the shutdown and finish its own
    # drain; it is linked so a crash here would already have propagated, this
    # is just to avoid leaking the peer task if it is still draining.
    receive do
      {:peer_done, ^peer} -> :ok
    after
      5_000 -> :ok
    end

    :gen_tcp.close(csock)
    :gen_tcp.close(usock)
  end

  # A dumb byte pump: read from `from_sock`, write to `to_sock`, repeat, until
  # `from_sock` errors/closes. {:active, false} + recv with a bounded size keeps
  # this a pull-based loop (no unbounded mailbox growth from an eager reader).
  # On EOF/error, shuts down `to_sock`'s WRITE half (half-close propagation: the
  # peer sees "no more bytes coming this way" without losing its own still-open
  # read direction) rather than a hard close, which the caller (pump_both) does
  # once both directions have finished.
  defp pump(from_sock, to_sock) do
    case :gen_tcp.recv(from_sock, 0, :infinity) do
      {:ok, data} ->
        case :gen_tcp.send(to_sock, data) do
          :ok ->
            pump(from_sock, to_sock)

          {:error, _reason} ->
            safe_shutdown(from_sock, :read)
        end

      {:error, :closed} ->
        safe_shutdown(to_sock, :write)

      {:error, _reason} ->
        safe_shutdown(to_sock, :write)
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

  defp workload_for_port(ctx, port) do
    Embervm.WorkloadCatalog.all_names(ctx.catalog_table)
    |> Enum.find(fn name ->
      case Embervm.WorkloadCatalog.fetch(ctx.catalog_table, name) do
        {:ok, %{class: "stateful", stateful: %{listen_port: ^port}}} -> true
        _ -> false
      end
    end)
  end
end
