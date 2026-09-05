defmodule Embervm.StoreFinch do
  @moduledoc """
  Dedicated Finch pool for the object store.

  The Kubernetes client intentionally trusts only the mounted service-account
  CA. This pool instead loads the operating system CA store through OTP and
  falls back to Wolfi's CA bundle path when OTP cannot find an OS store.
  """

  require Logger

  @system_ca_file "/etc/ssl/certs/ca-certificates.crt"

  @doc false
  def child_spec(opts) do
    {transport_opts, source} = transport_opts(opts)

    Logger.info("embervm store Finch trust configured from #{source}", ca_source: source)

    finch_spec =
      {Finch,
       name: __MODULE__,
       pools: %{default: [conn_opts: [transport_opts: transport_opts]]}}

    Supervisor.child_spec(finch_spec, id: __MODULE__)
  end

  @doc false
  def transport_opts(opts \\ []) do
    endpoint = Keyword.get(opts, :endpoint, "")
    cacerts_fun = Keyword.get(opts, :cacerts_fun, &:public_key.cacerts_get/0)
    ca_file = Keyword.get(opts, :cacertfile, @system_ca_file)

    {ca_opts, source} = system_ca_opts(cacerts_fun, ca_file)

    sni_opts =
      case URI.parse(endpoint) do
        %URI{scheme: "https", host: host} when is_binary(host) and host != "" ->
          [server_name_indication: String.to_charlist(host)]

        _ ->
          []
      end

    {[verify: :verify_peer, depth: 3] ++ ca_opts ++ sni_opts, source}
  end

  defp system_ca_opts(cacerts_fun, ca_file) do
    case cacerts_fun.() do
      cacerts when is_list(cacerts) and cacerts != [] -> {[cacerts: cacerts], "otp_os_store"}
      _ -> {[cacertfile: ca_file], ca_file}
    end
  rescue
    _ -> {[cacertfile: ca_file], ca_file}
  catch
    _, _ -> {[cacertfile: ca_file], ca_file}
  end
end
