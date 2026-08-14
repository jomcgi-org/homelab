defmodule Embervm.SpecTrace.Store do
  @moduledoc "Persistence behaviour for SpecTrace records."

  @callback write(GenServer.server(), [map()]) :: :ok | {:error, term()}
  @callback read_window(GenServer.server(), keyword()) :: {:ok, [map()]} | {:error, term()}
  @callback sweep(GenServer.server(), keyword()) ::
              {:ok, %{deleted: non_neg_integer(), done: boolean()}} | {:error, term()}

  @doc """
  The highest `seq` already stored, or 0 when empty.

  `seq` is the PRIMARY KEY. The writer is supervised, so it restarts on a crash,
  and starting its counter at 0 again means every insert collides with an
  existing row: the batch write errors, `flush/1` swallows it as a counted drop,
  and the trace silently records NOTHING until seq climbs past the old maximum.
  The facility reads as enabled the whole time.

  Resuming from here is what makes a writer restart survivable rather than a
  silent stop. See #4841.
  """
  @callback max_seq(GenServer.server()) :: non_neg_integer()
end
