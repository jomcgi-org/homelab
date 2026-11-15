defmodule Embervm.EnvoyStats do
  @moduledoc """
  Envoy admin `/stats?format=json` scraping shared by the sweepers. Envoy emits
  `{"stats":[{"name":..,"value":..}, ...]}`; each sweeper picks the counters it
  needs from that body.
  """

  @doc """
  GET `url` over the shared Finch pool and hand a 2xx body to `parse_fun`. A non-2xx
  status or a transport error is a scrape failure (fail-open upstream).
  """
  @spec scrape(String.t(), (binary() -> {:ok, term()} | {:error, term()})) :: {:ok, term()} | {:error, term()}
  def scrape(url, parse_fun) do
    req = Finch.build(:get, url)

    case Finch.request(req, Embervm.Finch, receive_timeout: 3_000) do
      {:ok, %Finch.Response{status: status, body: body}} when status in 200..299 ->
        parse_fun.(body)

      {:ok, %Finch.Response{status: status}} ->
        {:error, {:stats_status, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @tcp_stat_prefix "tcp."
  @cx_active_suffix ".downstream_cx_active"
  @cx_total_suffix ".downstream_cx_total"

  @doc """
  Parse a stats body into `%{stat_prefix => %{active: n, total: n}}`, keeping only
  `tcp.<prefix>.downstream_cx_{active,total}` counters. Stateful (state-<port>) and
  composite (group-<port>) entry listeners share this tcp.* namespace.
  """
  @spec parse_tcp_stats(binary()) ::
          {:ok, %{optional(String.t()) => %{active: integer(), total: integer()}}} | {:error, term()}
  def parse_tcp_stats(body) do
    case Jason.decode(body) do
      {:ok, %{"stats" => stats}} when is_list(stats) ->
        reading = Enum.reduce(stats, %{}, fn stat, acc -> fold_tcp_stat(stat, acc) end)
        {:ok, reading}

      _ ->
        {:error, :unparseable_stats}
    end
  rescue
    e -> {:error, {:stats_parse_raised, e}}
  end

  defp fold_tcp_stat(%{"name" => @tcp_stat_prefix <> rest, "value" => value}, acc) when is_integer(value) do
    cond do
      String.ends_with?(rest, @cx_active_suffix) ->
        prefix = String.replace_suffix(rest, @cx_active_suffix, "")
        put_field(acc, prefix, :active, value)

      String.ends_with?(rest, @cx_total_suffix) ->
        prefix = String.replace_suffix(rest, @cx_total_suffix, "")
        put_field(acc, prefix, :total, value)

      true ->
        acc
    end
  end

  defp fold_tcp_stat(_stat, acc), do: acc

  defp put_field(acc, prefix, field, value) do
    entry = Map.get(acc, prefix, %{active: 0, total: 0})
    Map.put(acc, prefix, Map.put(entry, field, value))
  end
end
