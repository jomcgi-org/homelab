defmodule Embervm.StoreTLSTest do
  use ExUnit.Case, async: true

  import ExUnit.CaptureLog

  alias Embervm.StoreFinch
  alias Embervm.StoreTLS

  test "store transport uses OTP system CAs and is distinct from the K8s pin" do
    fake_cacerts = [<<1, 2, 3>>]

    {store_opts, source} =
      StoreFinch.transport_opts(
        endpoint: "https://storage.googleapis.com",
        cacerts_fun: fn -> fake_cacerts end
      )

    ca_file = temp_path()
    File.write!(ca_file, "test")
    on_exit(fn -> File.rm(ca_file) end)

    assert {Finch, k8s_spec} = Embervm.K8s.finch_child_spec(ca_file)

    k8s_opts =
      k8s_spec
      |> Keyword.fetch!(:pools)
      |> Map.fetch!(:default)
      |> Keyword.fetch!(:conn_opts)
      |> Keyword.fetch!(:transport_opts)

    assert source == "otp_os_store"
    assert Keyword.fetch!(store_opts, :verify) == :verify_peer
    assert Keyword.fetch!(store_opts, :cacerts) == fake_cacerts
    assert Keyword.fetch!(store_opts, :depth) == 3
    assert Keyword.fetch!(store_opts, :server_name_indication) == ~c"storage.googleapis.com"
    refute Keyword.has_key?(store_opts, :cacertfile)

    assert Keyword.fetch!(k8s_opts, :cacertfile) == ca_file
    refute Keyword.has_key?(k8s_opts, :cacerts)
  end

  test "store transport falls back to the Wolfi bundle when OTP CA loading raises" do
    {opts, source} =
      StoreFinch.transport_opts(
        cacerts_fun: fn -> raise "no OS store" end,
        cacertfile: "/etc/ssl/certs/ca-certificates.crt"
      )

    assert source == "/etc/ssl/certs/ca-certificates.crt"
    assert Keyword.fetch!(opts, :cacertfile) == "/etc/ssl/certs/ca-certificates.crt"
    refute Keyword.has_key?(opts, :cacerts)
  end

  test "a completed 403 handshake is verified" do
    server =
      start_supervised!(
        {StoreTLS, name: nil, endpoint: "https://store.test", probe: fn -> {:ok, 403} end}
      )

    assert eventually(fn -> StoreTLS.snapshot(server) == %{store_tls: "verified"} end)
  end

  test "a TLS alert is failed and its last error is exposed" do
    error = {:tls_alert, {:unknown_ca, "certificate rejected"}}

    server =
      start_supervised!(
        {StoreTLS,
         name: nil, endpoint: "https://store.test", probe: fn -> {:error, error} end}
      )

    assert eventually(fn -> StoreTLS.snapshot(server).store_tls == "failed" end)
    snapshot = StoreTLS.snapshot(server)
    assert snapshot.store_tls_error =~ "unknown_ca"
  end

  test "a known TLS failure warns once per workload and refuses before retries" do
    error = {:tls_alert, {:unknown_ca, "certificate rejected"}}

    log =
      capture_log(fn ->
        server =
          start_supervised!(
            {StoreTLS,
             name: nil, endpoint: "https://store.test", probe: fn -> {:error, error} end}
          )

        assert eventually(fn -> StoreTLS.snapshot(server).store_tls == "failed" end)
        assert {:error, {:store_tls_failed, ^error}} = StoreTLS.permit_restore(server, "demo")
        assert {:error, {:store_tls_failed, ^error}} = StoreTLS.permit_restore(server, "demo")
      end)

    assert length(Regex.scan(~r/restore capability refused after store TLS probe failure/, log)) == 1
  end

  defp eventually(fun, attempts \\ 20)
  defp eventually(fun, 0), do: fun.()

  defp eventually(fun, attempts) do
    if fun.() do
      true
    else
      Process.sleep(5)
      eventually(fun, attempts - 1)
    end
  end

  defp temp_path do
    Path.join(System.tmp_dir!(), "embervm-k8s-ca-#{System.unique_integer([:positive])}.crt")
  end
end
