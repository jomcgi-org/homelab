defmodule Embervm.NodeAuth do
  @moduledoc """
  Builds the connection options shared by every control-plane dial to noded.

  The bearer token is loaded into application environment at boot. Keeping its
  transport representation here gives the three channel owners one mechanism
  to change if noded authentication evolves.
  """

  @spec connect_opts() :: keyword()
  def connect_opts do
    case Application.get_env(:embervm, :noded_bearer_token, "") do
      token when is_binary(token) ->
        case String.trim(token) do
          "" -> []
          trimmed -> [headers: [{"authorization", "Bearer " <> trimmed}]]
        end

      _ ->
        []
    end
  end
end
