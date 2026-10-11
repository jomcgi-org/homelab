defmodule Embervm.Scheduler.Request do
  @moduledoc """
  Fixed filters for one brick placement pass. `need_mib: nil` means no memory
  gate at all. It is not equivalent to zero, because zero still applies the
  brick's `mem_reject_floor_mib`. `record_demand: false` suppresses the
  autoscaler denial signal for a capacity miss and the unconfirmed
  empty-universe signal for a `:no_bricks` miss.

  `inflight` names the in-flight ledger (`Embervm.Scheduler.Inflight`) whose
  pending claims debit each candidate before filtering and scoring: `:default`
  is the shared table, an atom is a private table (tests), and `false` turns
  the debit off.
  """

  @typedoc """
  A placement request. Nil memory means no memory gate, not zero MiB.
  `record_demand` controls whether a capacity or empty-universe miss signals
  the autoscaler.
  """
  @type t :: %__MODULE__{
          bricks: [map()] | nil,
          table: atom() | nil,
          workload: term(),
          key: term(),
          need_mib: non_neg_integer() | nil,
          node_id: term() | nil,
          base: :none | :ready | {:ready, atom()},
          require_subnet: boolean(),
          record_demand: boolean(),
          inflight: :default | atom() | false
        }

  defstruct bricks: nil,
            table: nil,
            workload: nil,
            key: nil,
            need_mib: nil,
            node_id: nil,
            base: :none,
            require_subnet: false,
            record_demand: true,
            inflight: :default
end
