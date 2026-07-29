defmodule Embervm.CapacityObserver do
  @moduledoc """
  Read-only per-brick capacity measurements for the B5b-1 soak.

  This process emits one span and one structured log record per dispatchable
  brick on each interval. It does not participate in placement or admission.
  """

  use GenServer
  require Logger
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{Brick, BrickController, NodeCapacity}
  alias Embervm.Scheduler.Reservation

  @default_interval_ms 60_000

  @type record :: %{
          instance_id: term(),
          node_id: term(),
          size_class: term(),
          mem_budget_mib: non_neg_integer(),
          mem_headroom_mib: non_neg_integer(),
          mem_reserved_mib: non_neg_integer(),
          admits_on_reservation: boolean(),
          live_vms: non_neg_integer(),
          max_live_vms: non_neg_integer(),
          nameplate_mib: non_neg_integer() | nil,
          total_working_set_mib: integer() | nil,
          guest_free?: boolean(),
          cp_reserved_mib: non_neg_integer() | nil
        }

  @doc false
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc "Builds the current records without waiting for the observer timer."
  @spec records(atom(), atom()) :: [record()]
  def records(capacity_table \\ NodeCapacity.table(), reservation_table \\ Reservation.table()) do
    capacity_table
    |> Brick.bricks()
    |> Enum.map(&build_record(&1, reservation_table))
  end

  @doc "Builds one per-brick measurement record."
  @spec build_record(map(), atom()) :: record()
  def build_record(brick, reservation_table \\ Reservation.table()) do
    nameplate_mib = BrickController.nameplate_mib(Map.get(brick, :size_class, ""))
    headroom_mib = Map.get(brick, :mem_headroom_mib, 0)
    mem_reserved_mib = Map.get(brick, :mem_reserved_mib, 0)
    live_vms = Map.get(brick, :live_vms, 0)

    %{
      instance_id: Map.get(brick, :instance_id, ""),
      node_id: Map.get(brick, :node_id, ""),
      size_class: Map.get(brick, :size_class, ""),
      mem_budget_mib: Map.get(brick, :mem_budget_mib, 0),
      mem_headroom_mib: headroom_mib,
      mem_reserved_mib: mem_reserved_mib,
      admits_on_reservation: Map.get(brick, :admits_on_reservation, false),
      live_vms: live_vms,
      max_live_vms: Map.get(brick, :max_live_vms, 0),
      nameplate_mib: nameplate_mib,
      total_working_set_mib: if(is_integer(nameplate_mib), do: nameplate_mib - headroom_mib),
      guest_free?: mem_reserved_mib == 0 and live_vms == 0,
      cp_reserved_mib: reservation_mib(Map.get(brick, :instance_id, ""), reservation_table)
    }
  end

  @impl true
  def init(opts) do
    state = %{
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      reservation_table: Keyword.get(opts, :reservation_table, Reservation.table()),
      interval_ms: Keyword.get(opts, :interval_ms, @default_interval_ms)
    }

    schedule(state.interval_ms)
    {:ok, state}
  end

  @impl true
  def handle_info(:tick, state) do
    state.capacity_table
    |> records(state.reservation_table)
    |> Enum.each(&emit/1)

    schedule(state.interval_ms)
    {:noreply, state}
  end

  defp emit(record) do
    attributes =
      record
      |> Enum.map(fn {key, value} -> {Atom.to_string(key), value} end)
      |> Enum.reject(fn {_key, value} -> is_nil(value) end)
      |> Map.new()

    Tracer.with_span "embervm.capacity.brick", %{attributes: attributes} do
      :ok
    end

    Logger.info("embervm capacity brick", Map.to_list(record))
  end

  defp reservation_mib(instance_id, table) do
    Reservation.reserved_mib(instance_id, table)
  rescue
    ArgumentError -> nil
  end

  defp schedule(interval_ms), do: Process.send_after(self(), :tick, interval_ms)
end
