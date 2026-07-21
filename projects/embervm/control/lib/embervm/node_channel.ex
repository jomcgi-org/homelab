defmodule Embervm.NodeChannel do
  @moduledoc """
  A persistent, reused gRPC channel per node daemon, for the dispatch HOT path.

  `Embervm.BaseBuilder` and `Embervm.NodeRegistry` each open their OWN channel
  and can afford to: a base build is rare and a WatchNode stream is long-lived,
  so a per-operation connect amortizes to nothing. `Prime` and especially
  `Assign` are different: the warm-dispatch latency budget is p95 <= 25ms
  submit-to-Assign, and a fresh h2c connect + HTTP/2 handshake per assign would
  blow it on its own. So the pool and dispatcher share ONE long-lived channel
  per node, opened once and reused across every Prime/Assign/Destroy, with
  HTTP/2 stream multiplexing carrying concurrent RPCs over the single
  connection.

  ## why persistent_term, not a GenServer.call per fetch

  The channel handle changes only on a (re)dial, which is rare; it is read on
  every single dispatch, which is the hottest path in the system. That read:mostly
  -never-write ratio is exactly what `:persistent_term` is for: a worker reads its
  node's channel lock-free with no message round-trip, and the only writes (a dial
  or an invalidation) go through this GenServer and are infrequent enough that the
  global GC cost persistent_term imposes per write is irrelevant. A `GenServer.call`
  per fetch would serialize every concurrent assign worker through one process,
  reintroducing exactly the head-of-line coupling the channel pool exists to avoid.

  ## lazy dial, invalidate-on-failure

  Channels are dialed lazily on first `get/1` and cached. There is no proactive
  liveness monitor of the (adapter-internal) connection process; instead a worker
  that sees a TRANSPORT error on an RPC calls `invalidate/2`, which drops the dead
  channel so the next `get/1` re-dials. Invalidation is identity-guarded: it only
  clears the cache if the failing channel is still the current one, so a worker
  racing a fresh reconnect cannot tear down the newly dialed channel. Dials are
  serialized through this GenServer, so a thundering herd of first-dispatch workers
  triggers exactly one connect (the rest read the freshly cached channel).

  Registry note (artifact-decoupling Phase 2): the daemon boots with an EMPTY
  workload registry and the control plane PUSHES it over SyncRegistry on connect
  (see `Embervm.NodeRegistry`), so a real Prime/Assign only succeeds once that
  replay lands and a base is built. Until then this channel dials, caches, and
  serves the health-gated daemon connection without carrying real task traffic.
  """

  use GenServer
  require Logger

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  The current channel for `node_id`, dialing and caching it on first use. Reads
  `:persistent_term` lock-free on the fast path; falls through to a serialized
  dial only when no channel is cached (first use or just-invalidated). Returns
  `{:error, :unknown_node}` for a node this holder was not configured with.
  """
  @spec get(GenServer.server(), String.t()) :: {:ok, term()} | {:error, term()}
  def get(server \\ __MODULE__, node_id) do
    case :persistent_term.get(pt_key(node_id), :undefined) do
      :undefined -> GenServer.call(server, {:dial, node_id})
      channel -> {:ok, channel}
    end
  end

  @doc """
  Drop `node_id`'s cached channel if it is still `channel` (a worker observed a
  transport error on it), so the next `get/1` re-dials. Identity-guarded so a
  late invalidation cannot clobber a channel a concurrent reconnect already
  replaced. A cast: the reporting worker never blocks on teardown.
  """
  @spec invalidate(GenServer.server(), String.t(), term()) :: :ok
  def invalidate(server \\ __MODULE__, node_id, channel) do
    GenServer.cast(server, {:invalidate, node_id, channel})
  end

  @doc """
  Point `node_id` at a NEW `address` and drop any cached channel so the next
  `get/1` re-dials the new endpoint (artifact-decoupling PR-C, C4). The node_addr
  map seeded at init is STATIC; when a noded DaemonSet pod rolls, its stable
  node id keeps but its pod IP (address) changes, so `Embervm.NodeRegistry`'s
  re-discovery calls this to keep the Prime/Assign hot-path channel from dialing
  the dead old IP forever. Adds the node if it was unknown (a newly-discovered
  node). Synchronous so the caller knows the map is updated before it starts a
  fresh streamer against the same address.
  """
  @spec update_address(GenServer.server(), String.t(), String.t()) :: :ok
  def update_address(server \\ __MODULE__, node_id, address) do
    GenServer.call(server, {:update_address, node_id, address})
  end

  @doc """
  Drop `node_id` from the address map entirely and erase any cached channel, so a
  subsequent `get/1` returns `{:error, :unknown_node}` rather than re-dialing a
  dead endpoint. Used by `Embervm.NodeRegistry` when an instance expires: post-B0c an
  instance is registered under its instance_id (`"node/pod_uid"`) alone, so expiry
  removes that single key and no stale endpoint points at a torn-down pod's address.
  Idempotent: removing an unknown key is a no-op. Synchronous so the caller knows the
  map no longer resolves the key before it drops its own runtime entry.
  """
  @spec remove_address(GenServer.server(), String.t()) :: :ok
  def remove_address(server \\ __MODULE__, node_id) do
    GenServer.call(server, {:remove_address, node_id})
  end

  @doc """
  Whether `error` means the CHANNEL's transport is dead (so the cached channel
  must be invalidated and re-dialed), as opposed to a server-returned gRPC status
  that rode a HEALTHY channel (which must NOT tear the shared channel down, per
  D-R2.7.2).

  The subtle case this exists for: when a noded pod is replaced, its old
  connection breaks, and the Mint gRPC adapter surfaces that as a
  `%GRPC.RPCError{status: 2}` (UNKNOWN) whose message is a transport failure
  ("...the connection is closed"), NOT as a raw `Mint.TransportError`. Callers that
  treat every `%GRPC.RPCError{}` as a healthy-channel status therefore never
  invalidate, and the node wedges (all Prime/StartServing/Assign fail with
  "connection is closed" until the control plane restarts). This predicate catches
  that wrapped shape as well as raw transport errors, so every call site can decide
  invalidation consistently.
  """
  @spec transport_dead?(term()) :: boolean()
  def transport_dead?(%GRPC.RPCError{status: 2, message: msg}) when is_binary(msg),
    do: connection_closed_msg?(msg)

  def transport_dead?(%GRPC.RPCError{}), do: false
  def transport_dead?(:closed), do: true
  # Mint's transport error, matched structurally so this module needs no compile-time
  # dependency on the Mint struct definition.
  def transport_dead?(%{__struct__: Mint.TransportError}), do: true
  def transport_dead?({:error, reason}), do: transport_dead?(reason)
  def transport_dead?(_), do: false

  defp connection_closed_msg?(msg) do
    m = String.downcase(msg)
    String.contains?(m, "connection is closed") or String.contains?(m, "connection closed")
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    nodes = Keyword.get(opts, :nodes, [])
    connect_fun = Keyword.get(opts, :connect_fun, &default_connect/1)
    disconnect_fun = Keyword.get(opts, :disconnect_fun, &default_disconnect/1)

    node_addr = for %{id: id, address: address} <- nodes, into: %{}, do: {id, address}

    {:ok, %{node_addr: node_addr, connect_fun: connect_fun, disconnect_fun: disconnect_fun}}
  end

  @impl true
  def handle_call({:dial, node_id}, _from, state) do
    # Re-check the cache inside the serialized call: a herd of first-dispatch
    # workers all miss persistent_term and land here; the first dials, the rest
    # must see the now-cached channel rather than each opening another.
    case :persistent_term.get(pt_key(node_id), :undefined) do
      :undefined -> {:reply, do_dial(state, node_id), state}
      channel -> {:reply, {:ok, channel}, state}
    end
  end

  @impl true
  def handle_call({:update_address, node_id, address}, _from, state) do
    state = %{state | node_addr: Map.put(state.node_addr, node_id, address)}

    # Drop any channel cached against the OLD address so the next get/1 re-dials
    # the new endpoint. Unconditional erase (not identity-guarded): the address
    # changed, so whatever is cached is dialed at a stale endpoint and must go.
    case :persistent_term.get(pt_key(node_id), :undefined) do
      :undefined ->
        :ok

      channel ->
        :persistent_term.erase(pt_key(node_id))
        safe_disconnect(state, channel)
    end

    {:reply, :ok, state}
  end

  @impl true
  def handle_call({:remove_address, node_id}, _from, state) do
    # Erase any cached channel first (unconditional: the endpoint is going away),
    # then drop the address so get/1 falls through to {:error, :unknown_node}.
    case :persistent_term.get(pt_key(node_id), :undefined) do
      :undefined ->
        :ok

      channel ->
        :persistent_term.erase(pt_key(node_id))
        safe_disconnect(state, channel)
    end

    state = %{state | node_addr: Map.delete(state.node_addr, node_id)}
    {:reply, :ok, state}
  end

  @impl true
  def handle_cast({:invalidate, node_id, channel}, state) do
    case :persistent_term.get(pt_key(node_id), :undefined) do
      ^channel ->
        :persistent_term.erase(pt_key(node_id))
        safe_disconnect(state, channel)

      _ ->
        # Already replaced (or never cached): the failing channel is stale, so
        # there is nothing of ours to tear down.
        :ok
    end

    {:noreply, state}
  end

  @impl true
  def handle_info(_msg, state) do
    # The Mint connection process this GenServer dials is LINKED to it, so when
    # invalidate/terminate disconnects a channel, a normal-reason {:EXIT, pid,
    # :normal} is delivered here. It is expected and benign: invalidate already
    # erased and disconnected the channel, and the next get/1 re-dials. Swallow it
    # (and any other stray info) rather than let it hit the default handle_info and
    # log a spurious no_handle_info error report on every channel teardown.
    {:noreply, state}
  end

  @impl true
  def terminate(_reason, state) do
    # Drop every cached channel so a restart re-dials cleanly and no channel
    # outlives this holder in persistent_term.
    for {node_id, _addr} <- state.node_addr do
      case :persistent_term.get(pt_key(node_id), :undefined) do
        :undefined ->
          :ok

        channel ->
          :persistent_term.erase(pt_key(node_id))
          safe_disconnect(state, channel)
      end
    end

    :ok
  end

  # -- internals -------------------------------------------------------------

  defp do_dial(state, node_id) do
    case Map.get(state.node_addr, node_id) do
      nil ->
        {:error, :unknown_node}

      address ->
        case state.connect_fun.(address) do
          {:ok, channel} ->
            :persistent_term.put(pt_key(node_id), channel)
            {:ok, channel}

          {:error, reason} ->
            Logger.warning("embervm node channel: dial to #{node_id} (#{address}) failed: #{inspect(reason)}")
            {:error, reason}
        end
    end
  end

  defp safe_disconnect(state, channel) do
    try do
      state.disconnect_fun.(channel)
    rescue
      _ -> :ok
    catch
      _, _ -> :ok
    end

    :ok
  end

  defp pt_key(node_id), do: {__MODULE__, node_id}

  # Plaintext h2c over the Mint adapter, the pattern NodeRegistry/BaseBuilder use.
  # Auth is deferred in R0 (noded runs open; mesh policy is the layer), so no
  # bearer-token metadata is attached here; enabling it later is an additive
  # change to the per-RPC metadata, not to this dial.
  defp default_connect(address) do
    GRPC.Stub.connect(address, adapter: GRPC.Client.Adapters.Mint)
  end

  defp default_disconnect(channel) do
    _ = GRPC.Stub.disconnect(channel)
    :ok
  end
end
