defmodule Embervm.Scheduler.Request do
  @moduledoc """
  Fixed filters for one brick placement pass. `need_mib: nil` means no memory
  gate at all. It is not equivalent to zero, because zero still applies the
  brick's `mem_reject_floor_mib`.
  """

  @typedoc "A placement request. Nil memory means no memory gate, not zero MiB."
  @type t :: %__MODULE__{
          bricks: [map()] | nil,
          table: atom() | nil,
          workload: term(),
          key: term(),
          need_mib: non_neg_integer() | nil,
          node_id: term() | nil,
          base: :none | :ready | {:ready, atom()},
          require_subnet: boolean()
        }

  defstruct bricks: nil,
            table: nil,
            workload: nil,
            key: nil,
            need_mib: nil,
            node_id: nil,
            base: :none,
            require_subnet: false
end
