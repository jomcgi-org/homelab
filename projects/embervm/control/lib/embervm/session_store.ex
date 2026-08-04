defmodule Embervm.SessionStore do
  @moduledoc """
  ETS hot set over the op-log's durable `sessions` projection (R2), mirroring
  `Embervm.TaskStore`: every read the session hot path needs is O(1) against ETS,
  while every write goes through the op-log FIRST and only lands in ETS once the
  op-log confirms it is durable. That ordering, "op-log append succeeds, then and
  only then update ETS", is the write-through invariant this module enforces: ETS
  never shows a session in a state the op-log does not already agree with, and a
  crash between the two never loses a transition (worst case ETS is briefly stale
  until the next boot's rebuild replays it).

  On `init/1` this rebuilds its ETS table from `OpLog.load_sessions/1`, the
  recovery path: a fresh SessionStore against an existing op-log ends up with
  exactly the state the durable projection recorded, no replay logic beyond "read
  the projection". Adoption (PR-4, Task 8) layers the NODE's reported inventory on
  top of this durable rebuild to heal residency and limbo states.

  ## what it owns

    * the session hot set (`session_id -> session map`), the projected durable row
      plus the transient FSM state the SessionManager drives it through;
    * per-workload live/banked COUNTS, derived on every write so the create-time
      `maxSessions` gate is an O(1) read (never a table scan);
    * residency facts (`session_id -> {node_id, vm_id}` for a LIVE session), the
      lossy ETS the SessionManager and placement (PR-4) read to route an invoke to
      the VM without touching the durable store. Rebuilt from adoption, so it is
      allowed to be empty after a restart until the node reports.

  ## the token capability

  `verify_token/2` is the "who may hit this session" gate ADR embervm/001 names as
  distinct from management auth: it looks the session up BY id, then compares the
  presented token's sha256 CONSTANT-TIME against the stored hash (see
  `Embervm.SessionId`). A token for session S authenticates ONLY S: the lookup is
  by the id from the URL, so possession of S's token grants nothing on S'. A
  terminal session rejects every token (the capability dies with the session).
  """

  use GenServer

  alias Embervm.OpLog.Op
  alias Embervm.{SessionId, SessionState}

  @sessions_table :embervm_sessions
  @residency_table :embervm_session_residency

  # The live (non-terminal) session states, for residency and other non-terminal
  # checks; the per-workload COUNTS bucket a subset of these into `:banked`
  # instead (see `bucket_of/1`). `banked` is counted separately because it holds
  # disk, not a VM. `parked` also counts with banked: park tears the VM down and
  # nulls node_id/vm_id (session_manager.ex park_session), so a parked session
  # holds only its workspace volume, same as banked. It stays in `@live_states`
  # because it is still non-terminal and dialable through rejoin/relight, just not
  # a VM holder. `destroying` still holds a live VM (teardown RPC in flight, ADR
  # embervm/014 decision 5): it stays routable/dialable and counts against
  # capacity until the node confirms teardown and the terminal destroyed op fires.
  @live_states [:creating, :running, :banking, :parking, :relighting, :destroying, :parked]

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Creates a new session, minting its id and capability token. `attrs` is
  `%{tenant, principal, workload, node_id, vm_id, base_snapshot_ref, base_digest,
  expires_at}`. Appends `session_created` (write-through), inserts the ETS hot-set
  row in `:running` (the create claimed a live VM), records the residency fact,
  and returns `{:ok, %{session_id, token, expires_at, base_digest, ...}}`. The
  plaintext token is returned ONLY here and never stored.
  """
  @spec create(GenServer.server(), map()) :: {:ok, map()} | {:error, term()}
  def create(store \\ __MODULE__, attrs) do
    GenServer.call(store, {:create, attrs})
  end

  @doc """
  Applies an FSM transition to a session, appending the matching op write-through.
  `event` is a `SessionState` event; `op_kind` and `payload` describe the op.
  `updates` are extra fields merged into the ETS row (e.g. `snapshot_ref`,
  `generation`, `node_id`, `vm_id`) AFTER the durable append succeeds. Terminal
  transitions drop the residency fact and wake parked callers. Returns the updated
  session or `{:error, reason}` (including `{:illegal_transition, ...}`).
  """
  @spec transition(GenServer.server(), String.t(), atom(), atom(), map(), map()) ::
          {:ok, map()} | {:error, term()}
  def transition(store \\ __MODULE__, session_id, event, op_kind, payload, updates) do
    GenServer.call(store, {:transition, session_id, event, op_kind, payload, updates})
  end

  @doc """
  Like `transition/6`, but the durable append may be DEFERRED to Embervm.AsyncWriter
  when `EMBERVM_ASYNC_LIFECYCLE_WRITES` is on (ADR embervm/014 decision 2). Used only
  for the wake hot-path `session_relit`: the ETS row (state + residency +
  `node_id`/`vm_id` from `updates`) is advanced synchronously so the woken session is
  immediately routable and adoption sees it, and the durable append lands afterward.
  `vm_id` registers the pending write for the reconciler's adopt-and-backfill
  discriminator. Gate off: identical to `transition/6` (write-through).
  """
  @spec transition_lifecycle(GenServer.server(), String.t(), atom(), atom(), map(), map(), binary() | nil) ::
          {:ok, map()} | {:error, term()}
  def transition_lifecycle(store \\ __MODULE__, session_id, event, op_kind, payload, updates, vm_id) do
    GenServer.call(store, {:transition_lifecycle, session_id, event, op_kind, payload, updates, vm_id})
  end

  @doc """
  Applies a TRANSIENT FSM edge to a session WITHOUT an op-log append: the ETS-only
  move into a mid-operation state (`banking`, `relighting`) that a crash heals from
  node inventory rather than from the durable log (there is no `session_banking`/
  `session_relighting` op kind by design, standing decision: the durable log records
  only completed lifecycle transitions). `event` must be a legal FSM edge from the
  session's current state; an illegal edge is `{:error, {:illegal_transition, ...}}`.
  Keeps residency + per-workload counts consistent, exactly like `transition/6`, but
  never touches the op-log, so it must ONLY be used for states a later durable op
  (`session_banked`/`session_relit`) or adoption will resolve.
  """
  @spec mark(GenServer.server(), String.t(), atom()) :: {:ok, map()} | {:error, term()}
  def mark(store \\ __MODULE__, session_id, event) do
    GenServer.call(store, {:mark, session_id, event})
  end

  @doc """
  Records a `session_invoked` op (usage only, NO request/response bodies) against a
  running session: a non-state-changing write-through (running -> running) so it is
  NOT an FSM transition. Bumps `last_invoke_at` and charges usage to the quota cache
  and usage projection in the same op (D12.1). `usage` is `%{cpu_ms, peak_rss_mib,
  wall_ms, ...}` or nil (charges nothing). Returns `{:ok, session}` or an error.
  """
  @spec record_invoke(GenServer.server(), String.t(), map() | nil) ::
          {:ok, map()} | {:error, term()}
  def record_invoke(store \\ __MODULE__, session_id, usage) do
    GenServer.call(store, {:record_invoke, session_id, usage})
  end

  @doc "The session's hot-set row, or `:error` if unknown."
  @spec get(GenServer.server(), String.t()) :: {:ok, map()} | :error
  def get(store \\ __MODULE__, session_id) do
    GenServer.call(store, {:get, session_id})
  end

  @doc """
  The residency fact (`{node_id, vm_id}`) for a LIVE session, or `:error` if the
  session is not live or unknown. A lossy ETS read (rebuilt by adoption), so the
  caller must tolerate `:error` for a session the store believes live but whose
  fact a restart has not yet re-learned.
  """
  @spec residency(GenServer.server(), String.t()) :: {:ok, {String.t(), String.t()}} | :error
  def residency(store \\ __MODULE__, session_id) do
    GenServer.call(store, {:residency, session_id})
  end

  @doc """
  Whether `token` authenticates the session `session_id`: a constant-time hash
  compare against the stored sha256, guarded by "the session exists and is not
  terminal". Returns `{:ok, session}` for a valid token on a live session,
  `{:error, :terminal}` for a valid token on a terminal one (so the caller can
  410 with the recorded reason), `{:error, :unauthorized}` for a bad token, or
  `{:error, :not_found}`.
  """
  @spec verify_token(GenServer.server(), String.t(), String.t()) ::
          {:ok, map()} | {:error, :terminal | :unauthorized | :not_found}
  def verify_token(store \\ __MODULE__, session_id, token) do
    GenServer.call(store, {:verify_token, session_id, token})
  end

  @doc """
  Live and banked counts for `workload` (`%{live, banked}`), the O(1) create-time
  capacity read. Live is any non-terminal session that can hold a VM (excludes
  banked and parked); banked is the disk bucket, `banked` plus `parked` sessions,
  since both hold only a workspace volume and no VM. Reads the maintained
  per-workload counter, never a scan.
  """
  @spec counts(GenServer.server(), String.t()) :: %{live: non_neg_integer(), banked: non_neg_integer()}
  def counts(store \\ __MODULE__, workload) do
    GenServer.call(store, {:counts, workload})
  end

  @doc """
  Every session in the hot set (a full ETS scan), for the adoption reconcile that
  compares the durable projection against the node's reported inventory. Rare (boot
  + every registry sweep), never on the invoke path.
  """
  @spec all(GenServer.server()) :: [map()]
  def all(store \\ __MODULE__) do
    GenServer.call(store, :all)
  end

  @doc """
  Adoption: FORCE a session's ETS state to `new_state` (one of `running`/`banked`)
  from authoritative node truth, bypassing the FSM (adoption is idempotent and
  derived from what the node actually holds, so it must be total over limbo states
  the FSM cannot bridge, e.g. a `banking` session whose node reports a live VM).
  Updates residency + per-workload counts, drops residency for `banked`, and does
  NOT append an op (the durable log already recorded the last completed transition;
  adoption only re-derives the transient ETS view). A no-op for an unknown or
  already-terminal session (never resurrects a terminal row). Returns `:ok`.
  """
  @spec adopt_state(GenServer.server(), String.t(), :running | :banked) :: :ok
  def adopt_state(store \\ __MODULE__, session_id, new_state) do
    GenServer.call(store, {:adopt_state, session_id, new_state})
  end

  @doc """
  Adoption: rebind a session the node reports as a LIVE VM to `{node_id, vm_id}`,
  writing the residency fact and the ETS row's `vm_id`/`node_id` WITHOUT an FSM
  transition or an op-log append (residency is a lossy fact the node owns, not
  durable state). A no-op for an unknown session. Returns `:ok`.
  """
  @spec adopt_residency(GenServer.server(), String.t(), String.t(), String.t()) :: :ok
  def adopt_residency(store \\ __MODULE__, session_id, node_id, vm_id) do
    GenServer.call(store, {:adopt_residency, session_id, node_id, vm_id})
  end

  @doc """
  Re-drive the durable `session_created` append for a session whose ETS row
  survived but whose deferred async append was lost (ADR embervm/014 decision 2's
  adopt-and-backfill repair, called by the reconciler). Reconstructs the op from
  the live ETS row and appends it SYNCHRONOUSLY (reconcile is not a hot path, and a
  synchronous append guarantees the durable row lands before the next pass). The
  projection is INSERT OR IGNORE, so a re-drive that races the original append (or
  runs twice) is idempotent. A no-op / `:error` for an unknown session.
  """
  @spec backfill_created(GenServer.server(), String.t()) :: :ok | {:error, term()}
  def backfill_created(store \\ __MODULE__, session_id) do
    GenServer.call(store, {:backfill_created, session_id})
  end

  @doc """
  Pages sessions for `workload`, newest-created first, by `:limit` (default 50)
  and `:offset` (default 0). A full ETS scan (bounded, listing is rare), never
  the op-log; serves `GET /v1/workloads/{name}/sessions`.
  """
  @spec list(GenServer.server(), String.t(), keyword()) :: {:ok, map()}
  def list(store \\ __MODULE__, workload, opts \\ []) do
    GenServer.call(store, {:list, workload, opts})
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    # The backend module dispatched at every call site below, threaded alongside
    # :op_log (the server address) so a non-default backend never requires editing
    # this module. Defaults to the selected backend module.
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)
    clock = Keyword.get(opts, :clock, &default_clock/0)
    id_fun = Keyword.get(opts, :id_fun, nil)
    # Fired AFTER a session_invoked/other op that carried usage lands durably,
    # %{principal, ts, stats}, so Embervm.Metering charges the quota cache off the
    # same write, exactly like TaskStore. No-op default (unit tests wire none).
    on_metered = Keyword.get(opts, :on_metered, fn _event -> :ok end)
    # Off-hot-path boot/wake writes (ADR embervm/014 decision 2). When on, the
    # session_created (create) and session_relit (wake) durable appends are deferred
    # to Embervm.AsyncWriter AFTER the ETS row + token are minted synchronously (the
    # caller needs the token, and reads/adoption must see the row immediately), so
    # the boot/wake caller never blocks on the durable write. Off (default): exact
    # write-through ordering (append THEN reply). Never applies to session_invoked,
    # bank, or terminal ops (those stay synchronous).
    async_writer = Keyword.get(opts, :async_writer, Embervm.AsyncWriter)
    async_lifecycle_writes = Keyword.get(opts, :async_lifecycle_writes, false)

    sessions = :ets.new(@sessions_table, [:set, :private])
    residency = :ets.new(@residency_table, [:set, :private])

    state = %{
      op_log: op_log,
      op_log_mod: op_log_mod,
      clock: clock,
      id_fun: id_fun,
      on_metered: on_metered,
      async_writer: async_writer,
      async_lifecycle_writes: async_lifecycle_writes,
      sessions: sessions,
      residency: residency,
      # workload -> %{live, banked}, kept in step with the hot set on every write.
      counts: %{}
    }

    case rebuild(state) do
      {:ok, state} -> {:ok, state}
      {:error, reason} -> {:stop, {:rebuild_failed, reason}}
    end
  end

  # Rebuild: read every durable session row and populate the hot set + counts from
  # scratch. No per-op replay: the projection already IS the current state. The
  # residency table is NOT rebuilt here (it is a live-VM fact the node reports;
  # adoption in PR-4 fills it) so a fresh boot has empty residency until the node
  # is heard from, which is correct (the control plane must not assume a VM lives
  # where a stale durable node_id says without the node confirming).
  defp rebuild(state) do
    case state.op_log_mod.load_sessions(state.op_log) do
      {:ok, rows} ->
        state =
          Enum.reduce(rows, state, fn row, acc ->
            session = row_to_session(row)
            :ets.insert(acc.sessions, {session.session_id, session})
            bump_counts(acc, nil, session.state, session.workload)
          end)

        {:ok, state}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp row_to_session(row) do
    %{
      session_id: row.session_id,
      tenant: row.tenant,
      principal: row.principal,
      workload: row.workload,
      state: state_from_string(row.state),
      node_id: row.node_id,
      volume_node_id: row.volume_node_id,
      vm_id: nil,
      base_snapshot_ref: row.base_snapshot_ref,
      base_digest: row.base_digest,
      generation: row.generation || 0,
      snapshot_ref: row.snapshot_ref,
      snapshot_size_bytes: row.snapshot_size_bytes,
      token_sha256: row.token_sha256,
      created_at: row.created_at,
      last_invoke_at: row.last_invoke_at,
      expires_at: row.expires_at,
      updated_at: row.updated_at,
      terminal_reason: row.terminal_reason
    }
  end

  # Explicit map (not String.to_existing_atom): fails loudly on an unknown string
  # and documents the exact projection strings, exactly as TaskStore does.
  @state_strings %{
    "creating" => :creating,
    "running" => :running,
    "banking" => :banking,
    "parking" => :parking,
    "banked" => :banked,
    "parked" => :parked,
    "relighting" => :relighting,
    "destroying" => :destroying,
    "expired" => :expired,
    "evicted" => :evicted,
    "destroyed" => :destroyed,
    "failed" => :failed
  }

  defp state_from_string(str), do: Map.fetch!(@state_strings, str)

  @impl true
  def handle_call({:create, attrs}, _from, state) do
    do_create(attrs, state)
  end

  def handle_call({:transition, session_id, event, op_kind, payload, updates}, _from, state) do
    do_transition(state, session_id, event, op_kind, payload, updates)
  end

  def handle_call({:transition_lifecycle, session_id, event, op_kind, payload, updates, vm_id}, _from, state) do
    if state.async_lifecycle_writes do
      do_transition_async(state, session_id, event, op_kind, payload, updates, vm_id)
    else
      do_transition(state, session_id, event, op_kind, payload, updates)
    end
  end

  def handle_call({:mark, session_id, event}, _from, state) do
    do_mark(state, session_id, event)
  end

  def handle_call({:record_invoke, session_id, usage}, _from, state) do
    do_record_invoke(state, session_id, usage)
  end

  def handle_call({:get, session_id}, _from, state) do
    {:reply, get_view(state, session_id), state}
  end

  def handle_call({:residency, session_id}, _from, state) do
    case :ets.lookup(state.residency, session_id) do
      [{^session_id, {node_id, vm_id}}] -> {:reply, {:ok, {node_id, vm_id}}, state}
      [] -> {:reply, :error, state}
    end
  end

  def handle_call({:verify_token, session_id, token}, _from, state) do
    reply =
      case fetch(state, session_id) do
        {:ok, session} ->
          cond do
            not SessionId.verify_token(token, session.token_sha256) -> {:error, :unauthorized}
            SessionState.terminal?(session.state) -> {:error, :terminal}
            true -> {:ok, session}
          end

        {:error, {:not_found, _}} ->
          {:error, :not_found}
      end

    {:reply, reply, state}
  end

  def handle_call({:counts, workload}, _from, state) do
    {:reply, Map.get(state.counts, workload, %{live: 0, banked: 0}), state}
  end

  def handle_call(:all, _from, state) do
    all = :ets.foldl(fn {_id, session}, acc -> [session | acc] end, [], state.sessions)
    {:reply, all, state}
  end

  def handle_call({:backfill_created, session_id}, _from, state) do
    case fetch(state, session_id) do
      {:ok, session} ->
        # Reconstruct the session_created op from the surviving ETS row and append it
        # synchronously. The projection is INSERT OR IGNORE, so this is idempotent
        # even if the original deferred append later drains or this runs twice.
        op = %Op{
          kind: :session_created,
          tenant: session.tenant,
          principal: session.principal,
          workload: session.workload,
          session_id: session_id,
          ts: state.clock.(),
          payload: %{
            node_id: session.node_id,
            volume_node_id: session.volume_node_id,
            base_snapshot_ref: session.base_snapshot_ref,
            base_digest: session.base_digest,
            token_sha256: session.token_sha256,
            expires_at: session.expires_at,
            state: to_string(session.state)
          }
        }

        {:reply, backfill_reply(state.op_log_mod.append(state.op_log, op)), state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  def handle_call({:adopt_state, session_id, new_state}, _from, state)
      when new_state in [:running, :banked] do
    case fetch(state, session_id) do
      {:ok, %{state: cur}} when cur in [:expired, :evicted, :destroyed, :failed] ->
        # Never resurrect a terminal session from a stale node fact.
        {:reply, :ok, state}

      {:ok, session} ->
        if session.state == new_state do
          {:reply, :ok, state}
        else
          ts = state.clock.()
          updated = %{session | state: new_state, updated_at: ts}
          :ets.insert(state.sessions, {session_id, updated})
          state = bump_counts(state, session.state, new_state, session.workload)
          state = apply_residency(state, session.state, updated)
          {:reply, :ok, state}
        end

      {:error, _} ->
        {:reply, :ok, state}
    end
  end

  def handle_call({:adopt_residency, session_id, node_id, vm_id}, _from, state) do
    case fetch(state, session_id) do
      {:ok, session} ->
        updated = %{session | node_id: node_id, vm_id: vm_id}
        :ets.insert(state.sessions, {session_id, updated})
        :ets.insert(state.residency, {session_id, {node_id, vm_id}})
        {:reply, :ok, state}

      {:error, _} ->
        {:reply, :ok, state}
    end
  end

  def handle_call({:list, workload, opts}, _from, state) do
    limit = Keyword.get(opts, :limit, 50)
    offset = Keyword.get(opts, :offset, 0)

    all =
      :ets.foldl(
        fn {_id, session}, acc ->
          if session.workload == workload, do: [session | acc], else: acc
        end,
        [],
        state.sessions
      )
      |> Enum.sort_by(& &1.created_at, :desc)

    page = all |> Enum.drop(offset) |> Enum.take(limit)
    {:reply, {:ok, %{items: page, total: length(all), limit: limit, offset: offset}}, state}
  end

  # -- create ----------------------------------------------------------------

  defp do_create(attrs, state) do
    ts = state.clock.()
    session_id = Map.get(attrs, :session_id) || mint_id(state, ts)
    {token, token_sha256} = SessionId.mint_token()

    payload = %{
      node_id: Map.get(attrs, :node_id),
      volume_node_id: Map.get(attrs, :volume_node_id),
      base_snapshot_ref: Map.get(attrs, :base_snapshot_ref),
      base_digest: Map.get(attrs, :base_digest),
      token_sha256: token_sha256,
      expires_at: Map.get(attrs, :expires_at),
      # The durable projection defaults session_created to "running" (the create
      # already claimed a live VM); recorded explicitly for clarity.
      state: "running"
    }

    op = %Op{
      kind: :session_created,
      tenant: Map.fetch!(attrs, :tenant),
      principal: Map.get(attrs, :principal),
      workload: Map.fetch!(attrs, :workload),
      session_id: session_id,
      ts: ts,
      payload: payload
    }

    session = %{
      session_id: session_id,
      tenant: op.tenant,
      principal: op.principal,
      workload: op.workload,
      state: :running,
      node_id: Map.get(attrs, :node_id),
      vm_id: Map.get(attrs, :vm_id),
      volume_node_id: Map.get(attrs, :volume_node_id),
      base_snapshot_ref: Map.get(attrs, :base_snapshot_ref),
      base_digest: Map.get(attrs, :base_digest),
      generation: 0,
      snapshot_ref: nil,
      snapshot_size_bytes: nil,
      token_sha256: token_sha256,
      created_at: ts,
      last_invoke_at: nil,
      expires_at: Map.get(attrs, :expires_at),
      updated_at: ts,
      terminal_reason: nil
    }

    reply = %{
      session_id: session_id,
      token: token,
      expires_at: session.expires_at,
      base_digest: session.base_digest,
      state: :running
    }

    if state.async_lifecycle_writes do
      # Gate on (ADR embervm/014 decision 2): mint the ETS row + token synchronously
      # (the caller needs the token, and adoption/reads must see the session at
      # once), then defer the durable session_created append to Embervm.AsyncWriter
      # so the create caller does not block on it. A CP crash before the append
      # lands loses the row; the node still reports the live VM, and the adoption
      # backfill re-creates it (session_manager Direction-2), so the vm_id registers
      # the pending write for that discriminator.
      insert_created_session(state, session)

      Embervm.AsyncWriter.enqueue(state.async_writer, %{
        op: op,
        op_log_mod: state.op_log_mod,
        op_log: state.op_log,
        vm_id: Map.get(attrs, :vm_id)
      })

      {:reply, {:ok, reply}, bump_counts(state, nil, :running, session.workload)}
    else
      case state.op_log_mod.append(state.op_log, op) do
        {:ok, _seq} ->
          insert_created_session(state, session)
          {:reply, {:ok, reply}, bump_counts(state, nil, :running, session.workload)}

        {:error, _reason} = error ->
          {:reply, error, state}
      end
    end
  end

  # The ETS side of a create: the hot-set row + its residency fact. Shared by the
  # write-through and async paths so both land the identical in-memory state; only
  # the durable append's timing differs between them.
  defp insert_created_session(state, session) do
    :ets.insert(state.sessions, {session.session_id, session})
    put_residency(state, session)
    :ok
  end

  defp backfill_reply({:ok, _seq}), do: :ok
  defp backfill_reply({:error, _reason} = error), do: error

  # -- transition ------------------------------------------------------------

  defp do_transition(state, session_id, event, op_kind, payload, updates) do
    with {:ok, session} <- fetch(state, session_id),
         {:ok, next} <- SessionState.transition(session.state, event) do
      case append_and_update(state, session, op_kind, next, payload, updates) do
        {:ok, updated, state} -> {:reply, {:ok, updated}, state}
        {:error, reason} -> {:reply, {:error, reason}, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # The async-deferred transition (session_relit under the gate, ADR embervm/014
  # decision 2): advance the FSM + ETS row (state, merged updates, residency,
  # counts) SYNCHRONOUSLY so the woken session is immediately routable and adoption
  # sees it, then defer the durable append to Embervm.AsyncWriter. Mirrors
  # append_and_update's ETS side exactly, minus the metering hook: this path is only
  # ever a non-terminal, usage-free lifecycle op (session_relit), so there is
  # nothing to charge (metering stays synchronous, on the terminal/invoke ops).
  defp do_transition_async(state, session_id, event, op_kind, payload, updates, vm_id) do
    with {:ok, session} <- fetch(state, session_id),
         {:ok, next} <- SessionState.transition(session.state, event) do
      ts = state.clock.()

      op = %Op{
        kind: op_kind,
        tenant: session.tenant,
        principal: session.principal,
        workload: session.workload,
        session_id: session_id,
        ts: ts,
        payload: payload
      }

      updated =
        session
        |> Map.merge(updates)
        |> Map.merge(%{state: next, updated_at: ts})

      :ets.insert(state.sessions, {session_id, updated})
      state = bump_counts(state, session.state, next, session.workload)
      state = apply_residency(state, session.state, updated)

      Embervm.AsyncWriter.enqueue(state.async_writer, %{
        op: op,
        op_log_mod: state.op_log_mod,
        op_log: state.op_log,
        vm_id: vm_id
      })

      {:reply, {:ok, updated}, state}
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # Transient ETS-only FSM move (no op-log append): banking/relighting entry markers
  # a later completion op or adoption resolves. Keeps residency + counts in step with
  # the ETS state exactly like append_and_update, minus the durable write.
  defp do_mark(state, session_id, event) do
    with {:ok, session} <- fetch(state, session_id),
         {:ok, next} <- SessionState.transition(session.state, event) do
      ts = state.clock.()
      updated = %{session | state: next, updated_at: ts}
      :ets.insert(state.sessions, {session_id, updated})
      state = bump_counts(state, session.state, next, session.workload)
      state = apply_residency(state, session.state, updated)
      {:reply, {:ok, updated}, state}
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # session_invoked is running -> running (no FSM edge): a durable usage/last_invoke
  # write-through that does not move the state machine. Only a running session may
  # record an invoke (a banked/relighting session is not serving); a non-running
  # state is `{:error, :not_running}` so a race cannot journal an invoke against a
  # session that already banked or failed.
  defp do_record_invoke(state, session_id, usage) do
    case fetch(state, session_id) do
      {:ok, %{state: :running} = session} ->
        ts = state.clock.()
        payload = maybe_put_usage(%{}, usage)

        op = %Op{
          kind: :session_invoked,
          tenant: session.tenant,
          principal: session.principal,
          workload: session.workload,
          session_id: session_id,
          ts: ts,
          payload: payload
        }

        case state.op_log_mod.append(state.op_log, op) do
          {:ok, _seq} ->
            updated = %{session | last_invoke_at: ts, updated_at: ts}
            :ets.insert(state.sessions, {session_id, updated})
            notify_metered(state, updated, usage)
            {:reply, {:ok, updated}, state}

          {:error, _reason} = error ->
            {:reply, error, state}
        end

      {:ok, _session} ->
        {:reply, {:error, :not_running}, state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  # Bill raw usage stats into an op payload alongside the computed vcpu/gb-seconds,
  # matching TaskStore.maybe_put_usage; no :usage key for nil usage so the projection
  # stays a no-op. session_invoked's payload carries usage ONLY (no bodies).
  defp maybe_put_usage(payload, nil), do: payload

  defp maybe_put_usage(payload, stats) when is_map(stats) do
    Map.put(payload, :usage, Map.merge(stats, Embervm.Usage.billed(stats)))
  end

  # The write-through core: append to the op-log, and ONLY on {:ok, seq} update
  # ETS. On append failure ETS is left untouched (as durable as the op-log agrees)
  # and the error is returned. The `updates` map lets a bank/relight carry
  # snapshot_ref/generation/node_id/vm_id into the row atomically with the state
  # change. Terminal transitions record the reason, drop the residency fact, and
  # (for usage-carrying payloads) fire the metering hook.
  defp append_and_update(state, session, op_kind, next_state, payload, updates) do
    ts = state.clock.()

    op = %Op{
      kind: op_kind,
      tenant: session.tenant,
      principal: session.principal,
      workload: session.workload,
      session_id: session.session_id,
      ts: ts,
      payload: payload
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        terminal_reason =
          if SessionState.terminal?(next_state) do
            to_string(Map.get(payload, :reason, next_state))
          else
            session.terminal_reason
          end

        updated =
          session
          |> Map.merge(updates)
          |> Map.merge(%{state: next_state, updated_at: ts, terminal_reason: terminal_reason})

        :ets.insert(state.sessions, {session.session_id, updated})
        state = bump_counts(state, session.state, next_state, session.workload)
        state = apply_residency(state, session.state, updated)
        notify_metered(state, updated, Map.get(payload, :usage))

        {:ok, updated, state}

      {:error, reason} ->
        {:error, reason}
    end
  end

  # -- residency + counts ----------------------------------------------------

  # A LIVE session with a node+vm is routable; keep its fact. A banked/terminal
  # session has no VM: drop it. Called on every transition so the fact tracks the
  # live/not-live boundary exactly.
  defp apply_residency(state, _prior, session) do
    if session.state in @live_states and is_binary(session.node_id) and is_binary(session.vm_id) do
      :ets.insert(state.residency, {session.session_id, {session.node_id, session.vm_id}})
    else
      :ets.delete(state.residency, session.session_id)
    end

    state
  end

  defp put_residency(state, session) do
    apply_residency(state, nil, session)
  end

  # Maintain the per-workload {live, banked} counters as a session moves between
  # buckets. `prior` nil means "entering" (rebuild/create); otherwise decrement the
  # prior bucket and increment the next. A terminal state is neither live nor
  # banked, so it decrements without a matching increment.
  defp bump_counts(state, prior, next, workload) do
    counts = Map.get(state.counts, workload, %{live: 0, banked: 0})

    counts =
      counts
      |> dec_bucket(bucket_of(prior))
      |> inc_bucket(bucket_of(next))

    %{state | counts: Map.put(state.counts, workload, counts)}
  end

  defp bucket_of(nil), do: nil
  defp bucket_of(:banked), do: :banked
  defp bucket_of(:parked), do: :banked
  defp bucket_of(state) when state in @live_states, do: :live
  defp bucket_of(_terminal), do: nil

  defp inc_bucket(counts, nil), do: counts
  defp inc_bucket(counts, bucket), do: Map.update!(counts, bucket, &(&1 + 1))

  defp dec_bucket(counts, nil), do: counts
  defp dec_bucket(counts, bucket), do: Map.update!(counts, bucket, &max(&1 - 1, 0))

  # -- helpers ---------------------------------------------------------------

  defp fetch(state, session_id) do
    case :ets.lookup(state.sessions, session_id) do
      [{^session_id, session}] -> {:ok, session}
      [] -> {:error, {:not_found, session_id}}
    end
  end

  defp get_view(state, session_id) do
    case fetch(state, session_id) do
      {:ok, session} -> {:ok, session}
      {:error, _} -> :error
    end
  end

  # Mint an id: an injected id_fun (tests) or a ULID over the create timestamp.
  defp mint_id(%{id_fun: fun}, _ts) when is_function(fun, 0), do: fun.()
  defp mint_id(%{id_fun: fun}, ts) when is_function(fun, 1), do: fun.(ts)
  defp mint_id(_state, ts), do: SessionId.new(ts)

  # Best-effort, caught, fire-and-forget: a raise or missing hook never fails the
  # (already durable) transition, mirroring TaskStore.notify_metered.
  defp notify_metered(_state, _session, nil), do: :ok

  defp notify_metered(state, session, stats) when is_map(stats) do
    try do
      state.on_metered.(%{principal: session.principal, ts: session.updated_at, stats: stats})
    rescue
      _ -> :ok
    catch
      _, _ -> :ok
    end

    :ok
  end

  defp default_clock, do: System.system_time(:millisecond)
end
