defmodule Embervm.NodeAuth do
  @moduledoc """
  Builds the connection options shared by every control-plane dial to noded.

  The bearer token is loaded into application environment at boot. Keeping its
  transport representation here gives the three channel owners one mechanism
  to change if noded authentication evolves.
  """

  @connect_timeout_ms 3_000

  @doc "The maximum time a control-plane dial may spend establishing its TCP connection."
  @spec connect_timeout_ms() :: pos_integer()
  def connect_timeout_ms, do: @connect_timeout_ms

  @spec connect_opts() :: keyword()
  def connect_opts do
    [adapter_opts: [transport_opts: [timeout: @connect_timeout_ms]]] ++ auth_opts()
  end

  defp auth_opts do
    with token when is_binary(token) <- Application.get_env(:embervm, :noded_bearer_token, ""),
         trimmed when trimmed != "" <- String.trim(token) do
      [headers: [{"authorization", "Bearer " <> trimmed}]]
    else
      _ -> []
    end
  end
end
