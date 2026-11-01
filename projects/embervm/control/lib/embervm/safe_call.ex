defmodule Embervm.SafeCall do
  @moduledoc """
  Best-effort side effects whose failure must never crash the caller.
  """

  @doc "Run `fun`, turning any raise, throw or exit into `:error`."
  @spec run((-> term())) :: term()
  def run(fun) do
    fun.()
  rescue
    _ -> :error
  catch
    _, _ -> :error
  end
end
