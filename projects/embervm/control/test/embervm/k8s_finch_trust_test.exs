defmodule Embervm.K8sFinchTrustTest do
  @moduledoc """
  Regression coverage for the shared Finch pool's in-cluster trust store.
  """
  use ExUnit.Case, async: true

  alias Embervm.K8s

  @system_ca_file "/etc/ssl/certs/ca-certificates.crt"
  @tls_fixture_dir Path.expand("../fixtures", __DIR__)
  # Long-lived localhost-only fixture from Bandit's own TLS test support.
  @server_ca Path.join(@tls_fixture_dir, "store_tls_ca.pem")
  @server_cert Path.join(@tls_fixture_dir, "store_tls_server.pem")
  @server_key Path.join(@tls_fixture_dir, "store_tls_server_key.pem")
  @sa_ca_pem """
  -----BEGIN CERTIFICATE-----
  MIIBsTCCARoCCQC9Dv27jVwCUTANBgkqhkiG9w0BAQsFADAdMRswGQYDVQQDDBJl
  bWJlcnZtLXRlc3Qtc2EtY2EwHhcNMjYwOTAxMDU1MTIxWhcNMzYwODI5MDU1MTIx
  WjAdMRswGQYDVQQDDBJlbWJlcnZtLXRlc3Qtc2EtY2EwgZ8wDQYJKoZIhvcNAQEB
  BQADgY0AMIGJAoGBANtXm9vTnq6+bkKcg/+PdEZMW0I5R1yeDRpLOp8Gess/PJ0v
  251YZjbhSIYcppjq4DIHxQMrwHTAqL+Q7DBc0CiTuPzTo6cyW+oweXnE1/W7nWkk
  mh29J6jjZiW/V4UA+Z94/KypeJmu5yMJnTrTPq49/WSoT/jnDIm011JUElBnAgMB
  AAEwDQYJKoZIhvcNAQELBQADgYEAjtOP0j2j2YiODYd/o5slGALbTLOsoA7gW53y
  SP3roitsu9CA03IjG6qAB838JbSj9pi5T6WaRpmb0UScipZ35nFyW8uEPWttz0S2
  DnAlVRZvqI74T6rZF/Zss4u5cZXeZgl4CLBWgGMZmZNE2L6Okj00BEDXDSLoXt0y
  i2dkWoM=
  -----END CERTIFICATE-----
  """

  describe "finch_child_spec/1" do
    test "combines the system trust store with every SA CA certificate" do
      ca_file = temp_ca_file(@sa_ca_pem <> @sa_ca_pem)
      pools = pools(ca_file)
      assert Map.has_key?(pools, :default)

      transport_opts = transport_opts(ca_file)
      [sa_der] = pem_cacerts(@sa_ca_pem)
      cacerts = Keyword.fetch!(transport_opts, :cacerts)

      assert Keyword.fetch!(transport_opts, :verify) == :verify_peer
      refute Keyword.has_key?(transport_opts, :cacertfile)
      assert cacerts != []
      assert Enum.all?(cacerts, &is_binary/1)
      assert sa_der in cacerts
      assert Enum.count(cacerts, &(&1 == sa_der)) == 1

      case system_cacerts() do
        [] ->
          :ok

        system_cacerts ->
          assert Enum.any?(system_cacerts, &(&1 in cacerts))
          assert length(cacerts) > 1
      end
    end

    test "uses empty pools when the SA CA file does not exist" do
      missing = temp_path()
      refute File.exists?(missing)

      assert {Finch, opts} = K8s.finch_child_spec(missing)
      assert Keyword.fetch!(opts, :pools) == %{}
    end

    test "skips a malformed PEM entry without discarding valid SA certificates" do
      malformed = """
      -----BEGIN CERTIFICATE-----
      not-base64
      -----END CERTIFICATE-----
      """

      ca_file = temp_ca_file(@sa_ca_pem <> malformed)
      cacerts = ca_file |> transport_opts() |> Keyword.fetch!(:cacerts)
      [sa_der] = pem_cacerts(@sa_ca_pem)

      assert sa_der in cacerts
      assert MapSet.new(cacerts) == MapSet.new(system_cacerts() ++ [sa_der])
    end

    test "the configured pool accepts a trusted TLS peer and rejects untrusted or wrong-host peers" do
      port = start_tls_server()
      trusted = start_finch(@server_ca)
      untrusted = start_finch(temp_ca_file(@sa_ca_pem))

      assert {:ok, %Finch.Response{status: 404}} =
               Finch.build(:get, "https://localhost:#{port}/embervm/probe/.keep")
               |> Finch.request(trusted, receive_timeout: 2_000)

      assert {:error, untrusted_error} =
               Finch.build(:get, "https://localhost:#{port}/embervm/probe/.keep")
               |> Finch.request(untrusted, receive_timeout: 2_000)

      assert inspect(untrusted_error) =~ ~r/unknown_ca|certificate/i

      assert {:error, hostname_error} =
               Finch.build(:get, "https://127.0.0.1:#{port}/embervm/probe/.keep")
               |> Finch.request(trusted, receive_timeout: 2_000)

      assert inspect(hostname_error) =~ ~r/hostname|name check/i
    end
  end

  defp start_finch(ca_file) do
    name = String.to_atom("finch_trust_test_#{System.unique_integer([:positive, :monotonic])}")
    assert {Finch, opts} = K8s.finch_child_spec(ca_file)
    spec = Supervisor.child_spec({Finch, Keyword.put(opts, :name, name)}, id: name)
    start_supervised!(spec)
    name
  end

  defp start_tls_server do
    opts = [
      certfile: String.to_charlist(@server_cert),
      keyfile: String.to_charlist(@server_key),
      reuseaddr: true,
      active: false
    ]

    assert {:ok, listener} = :ssl.listen(0, opts)
    assert {:ok, {_address, port}} = :ssl.sockname(listener)
    pid = spawn(fn -> tls_accept_loop(listener) end)

    on_exit(fn ->
      :ssl.close(listener)
      if Process.alive?(pid), do: Process.exit(pid, :kill)
    end)

    port
  end

  defp tls_accept_loop(listener) do
    case :ssl.transport_accept(listener) do
      {:ok, socket} ->
        spawn(fn -> serve_tls_socket(socket) end)
        tls_accept_loop(listener)

      {:error, :closed} ->
        :ok
    end
  end

  defp serve_tls_socket(socket) do
    case :ssl.handshake(socket, 2_000) do
      {:ok, socket} ->
        _ = :ssl.recv(socket, 0, 2_000)
        :ok = :ssl.send(socket, "HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
        :ssl.close(socket)

      {:error, _reason} ->
        :ssl.close(socket)
    end
  end

  defp pools(ca_file) do
    assert {Finch, opts} = K8s.finch_child_spec(ca_file)
    Keyword.fetch!(opts, :pools)
  end

  defp transport_opts(ca_file) do
    ca_file
    |> pools()
    |> Map.fetch!(:default)
    |> Keyword.fetch!(:conn_opts)
    |> Keyword.fetch!(:transport_opts)
  end

  defp system_cacerts do
    case os_cacerts() do
      [] ->
        case File.read(@system_ca_file) do
          {:ok, pem} -> pem_cacerts(pem)
          {:error, _reason} -> []
        end

      cacerts ->
        cacerts
    end
  end

  defp os_cacerts do
    :public_key.cacerts_get()
    |> Enum.flat_map(&certificate_der/1)
  rescue
    _ -> []
  catch
    _, _ -> []
  end

  defp certificate_der(der) when is_binary(der), do: [der]
  defp certificate_der({:cert, der, _decoded}) when is_binary(der), do: [der]
  defp certificate_der(_entry), do: []

  defp pem_cacerts(pem) do
    for {:Certificate, der, _encryption} <- :public_key.pem_decode(pem), do: der
  end

  defp temp_ca_file(contents) do
    path = temp_path()
    File.write!(path, contents)
    on_exit(fn -> File.rm(path) end)
    path
  end

  defp temp_path do
    Path.join(System.tmp_dir!(), "embervm-sa-ca-#{System.unique_integer([:positive])}.crt")
  end
end
