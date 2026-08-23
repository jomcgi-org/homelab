defmodule Embervm.CustomerKMSTest do
  use ExUnit.Case, async: true

  alias Embervm.CustomerKMS

  test "parses a secret-backed HTTPS principal map" do
    raw =
      Jason.encode!(%{
        principals: %{
          "acct:alice" => %{
            endpoint: "https://kms.example/tenant/alice/",
            key_ref: "key-1",
            bearer_token: "grant"
          }
        }
      })

    assert {:ok,
            %{
              "acct:alice" => %{
                adapter: Embervm.CustomerKMS.HTTP,
                mode: :customer,
                transition_from: nil,
                endpoint: "https://kms.example/tenant/alice",
                key_ref: "key-1",
                bearer_token: "grant"
              }
            }} = CustomerKMS.parse_config(raw)
  end

  test "empty configuration is inert" do
    assert {:ok, %{}} = CustomerKMS.parse_config(nil)
    assert {:ok, %{}} = CustomerKMS.parse_config("")
  end

  test "rejects plaintext, credential-bearing, and malformed endpoints" do
    for endpoint <- [
          "http://kms.example",
          "https://user:pass@kms.example",
          "https://kms.example?token=secret",
          "not-a-url"
        ] do
      raw =
        Jason.encode!(%{
          principals: %{
            "acct:alice" => %{
              endpoint: endpoint,
              key_ref: "key-1",
              bearer_token: "grant"
            }
          }
        })

      assert {:error, _reason} = CustomerKMS.parse_config(raw)
    end
  end

  test "rejects missing principals and empty grants" do
    assert {:error, :invalid_customer_kms_config} = CustomerKMS.parse_config("{}")

    raw =
      Jason.encode!(%{
        principals: %{
          "acct:alice" => %{
            endpoint: "https://kms.example",
            key_ref: "key-1",
            bearer_token: ""
          }
        }
      })

    assert {:error, :invalid_customer_kms_config} = CustomerKMS.parse_config(raw)
  end

  test "parses only explicit opposite-mode custody transitions" do
    for {mode, transition_from, expected} <- [
          {"customer", "platform", {:customer, :platform}},
          {"platform", "customer", {:platform, :customer}},
          {"platform", nil, {:platform, nil}}
        ] do
      raw =
        Jason.encode!(%{
          principals: %{
            "acct:alice" => %{
              mode: mode,
              transition_from: transition_from,
              endpoint: "https://kms.example",
              key_ref: "key-1",
              bearer_token: "grant"
            }
          }
        })

      assert {:ok, %{"acct:alice" => config}} = CustomerKMS.parse_config(raw)
      assert {config.mode, config.transition_from} == expected
    end

    for {mode, transition_from} <- [
          {"customer", "customer"},
          {"platform", "platform"},
          {"unknown", nil},
          {"customer", "unknown"}
        ] do
      raw =
        Jason.encode!(%{
          principals: %{
            "acct:alice" => %{
              mode: mode,
              transition_from: transition_from,
              endpoint: "https://kms.example",
              key_ref: "key-1",
              bearer_token: "grant"
            }
          }
        })

      assert {:error, :invalid_custody_transition} = CustomerKMS.parse_config(raw)
    end
  end
end
