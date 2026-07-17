defmodule Embervm.ActivatorSplices do
  @moduledoc """
  A per-workload live-splice counter for composite groups (R5, Task 8): the ONE
  activity signal Envoy cannot see. `Embervm.TcpActivator` splices a client
  connection to a group's entry member in an ephemeral, unsupervised handler
  process; a long-lived session that BEGAN during a wake (the activator dialed the
  entry member directly, the byte pump runs for the life of the session) never
  re-enters the node Envoy's `downstream_cx_active` counter for the entry listener,
  so a group with a live activator splice can read `cx_active == 0` on the entry
  listener and LOOK idle when it is not.

  `Embervm.GroupSweeper`'s idle predicate must NOT bank a group with a live
  activator splice (severing it would drop the session mid-stream, the exact case
  standing decision 7 forbids), so this module tracks the control plane's OWN count
  of in-flight splices per workload. It is a plain `:ets.update_counter` table (a
  monotone increment on splice start, a decrement on splice end): the sweeper reads
  `live?/2`, the activator brackets each composite splice with `incr/2` .. `decr/2`.

  ## why an explicit counter, not a process registry

  A `Registry` keyed by workload would also count live splices, but every splice
  handler would have to register + hold a monitored entry, and the sweeper would
  scan the registry per tick. A single ETS counter cell per workload is cheaper on
  both the hot splice path (one `update_counter`) and the sweep read (one lookup),
  and it never leaks a stale entry the way a crashed-before-unregister process could
  (see the crash note below).

  ## crash safety: the decrement is bracketed, a crash is a NET OVER-count

  The activator brackets a composite splice with `incr` before the byte pump and
  `decr` in an `after`, so a normal splice teardown decrements exactly once. A
  handler CRASH between `incr` and the `after` would leak one count (the cell reads
  one-too-high forever). That error is SAFE for warmth: an over-count only ever
  keeps a group from banking (it reads busier than it is), never banks a busy group.
  The counter is clamped at zero on decrement so an over-decrement (double-`decr`, or
  a `decr` racing a fresh cell) can never drive it negative and thus never FAKE
  idleness. The fail-direction is deliberately toward warmth, exactly the sweeper's
  scrape-fails-open posture: when in doubt, do not bank.

  ## test seam

  `start_link/1` names the table (default `Embervm.ActivatorSplices`), so a test can
  run an isolated counter (`name: :"splices_\#{n}"`) and inject it into both the
  activator and the sweeper. `live?/2` on an unstarted or unknown table reads
  `false` (no splice known), the safe default for a sweeper wired without an
  activator (production always starts the table; a unit test of the sweeper alone
  can omit it and every group simply reads no-splice).
  """

  use GenServer

  @table __MODULE__

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    # The process is named after its TABLE (the counter's identity), not a fixed
    # module name: a test starts an isolated counter under `table: :splices_<n>`
    # alongside the app's own `Embervm.ActivatorSplices` counter without a name
    # collision (the reads go by table name, so the process name only needs to be
    # unique-per-table). An explicit `name: nil` starts an unnamed process.
    name = Keyword.get(opts, :name, Keyword.get(opts, :table, @table))
    GenServer.start_link(__MODULE__, opts, name: name)
  end

  @doc """
  The ETS table name a splice counter uses. `start_link` with `table: :foo` (or the
  default) creates a public named table `:foo`; `incr/2`, `decr/2`, and `live?/2`
  take that same table name so a test can point the activator and the sweeper at one
  isolated counter.
  """
  @spec table(GenServer.server()) :: atom()
  def table(server \\ __MODULE__), do: GenServer.call(server, :table)

  @doc """
  Increment the live-splice count for `workload` on `table`. Called by the activator
  the instant a composite splice's byte pump begins. A first splice for a workload
  creates the cell at 1. A nil/absent table is a no-op (a control plane wired
  without a splice counter never blocks banking on a signal it does not track).
  """
  @spec incr(atom(), String.t()) :: :ok
  def incr(table, workload) when is_atom(table) and is_binary(workload) do
    if table_exists?(table) do
      _ = :ets.update_counter(table, workload, {2, 1}, {workload, 0})
      :ok
    else
      :ok
    end
  end

  @doc """
  Decrement the live-splice count for `workload`, clamped at zero (an
  over-decrement can never fake idleness). Called by the activator in the `after`
  that brackets a composite splice, so a normal teardown returns the cell to its
  pre-splice value. A nil/absent table is a no-op.
  """
  @spec decr(atom(), String.t()) :: :ok
  def decr(table, workload) when is_atom(table) and is_binary(workload) do
    if table_exists?(table) do
      # {2, -1, 0, 0}: decrement position 2 by 1, but a threshold of 0 with a
      # set-value of 0 clamps at zero (update_counter's threshold semantics), so the
      # count never goes negative and never fakes idleness.
      _ = :ets.update_counter(table, workload, {2, -1, 0, 0}, {workload, 0})
      :ok
    else
      :ok
    end
  end

  @doc """
  Whether `workload` currently has ANY live activator splice on `table` (count > 0).
  The sweeper's idle predicate ANDs `not live?/2` with Envoy's `cx_active == 0` +
  flat `cx_total` before banking. An unstarted/unknown table reads `false` (no
  splice known), the safe default for a unit test of the sweeper alone.
  """
  @spec live?(atom(), String.t()) :: boolean()
  def live?(table, workload) when is_atom(table) and is_binary(workload) do
    if table_exists?(table) do
      case :ets.lookup(table, workload) do
        [{^workload, n}] -> n > 0
        [] -> false
      end
    else
      false
    end
  end

  defp table_exists?(table), do: :ets.whereis(table) != :undefined

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    table = Keyword.get(opts, :table, @table)
    # Public named table so the hot splice path (activator handler processes) and the
    # sweeper's read both reach it directly, no GenServer call on the byte-pump path.
    ^table = :ets.new(table, [:set, :public, :named_table, {:write_concurrency, true}])
    {:ok, %{table: table}}
  end

  @impl true
  def handle_call(:table, _from, state), do: {:reply, state.table, state}
end
