defmodule Embervm.K8sFinchTrustTest do
  @moduledoc """
  Regression coverage for the Kubernetes Finch pool's in-cluster CA pin.
  """
  use ExUnit.Case, async: true

  alias Embervm.K8s

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
    test "pins the Kubernetes pool to the service-account CA file" do
      ca_file = temp_ca_file(@sa_ca_pem <> @sa_ca_pem)
      pools = pools(ca_file)
      assert Map.has_key?(pools, :default)

      transport_opts = transport_opts(ca_file)

      assert Keyword.fetch!(transport_opts, :verify) == :verify_peer
      assert Keyword.fetch!(transport_opts, :cacertfile) == ca_file
      refute Keyword.has_key?(transport_opts, :cacerts)
    end

    test "uses empty pools when the SA CA file does not exist" do
      missing = temp_path()
      refute File.exists?(missing)

      assert {Finch, opts} = K8s.finch_child_spec(missing)
      assert Keyword.fetch!(opts, :pools) == %{}
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
