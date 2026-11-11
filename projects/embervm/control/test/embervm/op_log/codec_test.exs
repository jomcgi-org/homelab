defmodule Embervm.OpLog.CodecTest do
  use ExUnit.Case, async: true

  alias Embervm.OpLog.Codec

  test "payload encoding is byte-exact ETF and decoding preserves the stored normalization" do
    body = <<137, 0, 255, "body">>

    payload = %{
      "string" => %{child: :ready, absent: nil},
      body: body,
      list: [nil, :ready, true, false, %{keep: 42, drop: nil}, [body]],
      absent: nil,
      enabled: true,
      disabled: false
    }

    bytes = Codec.encode_payload(payload)
    assert bytes == :erlang.term_to_binary(payload)

    assert Codec.decode_payload(bytes) == %{
             "string" => %{"child" => "ready"},
             "body" => body,
             "list" => ["nil", "ready", true, false, %{"keep" => 42}, [body]],
             "enabled" => true,
             "disabled" => false
           }

    assert Codec.encode_payload(%{}) == :erlang.term_to_binary(%{})
  end

  test "legacy JSON remains a direct JSON decode, including null values" do
    json = ~s({"absent":null,"nested":{"absent":null},"list":[null,true,false],"body":"a\\u0000b"})
    assert Codec.decode_payload(json) == :json.decode(json)
    assert Codec.decode_payload(json)["absent"] == :null
    assert Codec.decode_payload(json)["nested"] == %{"absent" => :null}
    assert Codec.decode_payload(json)["list"] == [:null, true, false]
    assert Codec.decode_payload(json)["body"] == <<"a", 0, "b">>
    assert Codec.decode_payload("[null,true]") == [:null, true]
    assert Codec.decode_payload("null") == :null
  end

  test "payload decoding keeps permissive ETF terms and rejects malformed bytes" do
    assert Codec.decode_payload(:erlang.term_to_binary([nil, :ready])) == ["nil", "ready"]
    assert Codec.decode_payload(:erlang.term_to_binary(nil)) == "nil"

    for bytes <- [<<131>>, <<131, 255>>] do
      assert_raise ArgumentError, fn -> Codec.decode_payload(bytes) end
    end

    for {bytes, reason} <- [{"", :unexpected_end}, {"{", :unexpected_end}, {"not json", {:invalid_byte, 111}}] do
      error = assert_raise ErlangError, fn -> Codec.decode_payload(bytes) end
      assert error.original == reason
    end

    for input <- [nil, %{}, [], 42] do
      assert_raise FunctionClauseError, fn -> Codec.decode_payload(input) end
    end

    for input <- [nil, [], 42, "payload"] do
      assert_raise FunctionClauseError, fn -> Codec.encode_payload(input) end
    end
  end

  test "headers round-trip JSON objects and encode empty or non-map inputs as nil" do
    headers = %{"content-type" => "application/octet-stream", "x-test" => "yes"}
    encoded = Codec.encode_headers(headers)
    assert is_binary(encoded)
    assert :json.decode(encoded) == headers
    assert Codec.decode_headers(encoded) == headers

    for input <- [%{}, nil, [], "headers", 42] do
      assert Codec.encode_headers(input) == nil
    end
  end

  test "absent, malformed and non-object headers decode as an empty map" do
    for input <- [nil, "", "{", <<255>>, "[]", "null", "true", "42", ~s("text"), %{}, [], 42] do
      assert Codec.decode_headers(input) == %{}
    end

    assert Codec.decode_headers(~s({"nullable":null})) == %{"nullable" => :null}
  end

  test "invalid header map encoding still raises" do
    assert_raise ErlangError, fn -> Codec.encode_headers(%{"body" => <<255>>}) end
  end
end
