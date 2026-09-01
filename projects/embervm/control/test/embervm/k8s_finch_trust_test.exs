defmodule Embervm.K8sFinchTrustTest do
  @moduledoc """
  Regression coverage for the shared Finch pool's in-cluster trust store.
  """
  use ExUnit.Case, async: true

  alias Embervm.K8s

  @system_ca_file "/etc/ssl/certs/ca-certificates.crt"
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
