defmodule Embervm.SpecTrace.Store do
  @moduledoc "Persistence behaviour for SpecTrace records."

  @callback write(GenServer.server(), [map()]) :: :ok | {:error, term()}
  @callback read_window(GenServer.server(), keyword()) :: {:ok, [map()]} | {:error, term()}
  @callback sweep(GenServer.server(), keyword()) ::
              {:ok, %{deleted: non_neg_integer(), done: boolean()}} | {:error, term()}
end
