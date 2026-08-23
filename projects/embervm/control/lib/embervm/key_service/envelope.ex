defmodule Embervm.KeyService.Envelope do
  @moduledoc """
  Versioned envelope for a 32-byte data key wrapped by `Embervm.KeyService`.

  Version 1 records a platform-managed KEK epoch alongside the AES-GCM nonce,
  authentication tag, and wrapped key. Version 2 records a customer KMS key
  reference and an opaque wrapped key. The customer KMS protocol must bind the
  principal and key reference into its authenticated encryption context.
  """

  @enforce_keys [:principal, :wrapped_key]
  defstruct version: 1,
            principal: nil,
            epoch: nil,
            nonce: nil,
            tag: nil,
            key_ref: nil,
            wrapped_key: nil

  @type platform_envelope :: %__MODULE__{
          version: 1,
          principal: String.t(),
          epoch: non_neg_integer(),
          nonce: <<_::96>>,
          tag: <<_::128>>,
          key_ref: nil,
          wrapped_key: <<_::256>>
        }

  @type customer_envelope :: %__MODULE__{
          version: 2,
          principal: String.t(),
          epoch: nil,
          nonce: nil,
          tag: nil,
          key_ref: String.t(),
          wrapped_key: binary()
        }

  @type t :: platform_envelope() | customer_envelope()

  @spec encode(t()) :: binary()
  def encode(%__MODULE__{
        version: 1,
        principal: principal,
        epoch: epoch,
        nonce: nonce,
        tag: tag,
        wrapped_key: wrapped_key
      })
      when is_binary(principal) and byte_size(principal) > 0 and
             byte_size(principal) <= 4_294_967_295 and
             is_integer(epoch) and epoch >= 0 and epoch <= 18_446_744_073_709_551_615 and
             byte_size(nonce) == 12 and byte_size(tag) == 16 and byte_size(wrapped_key) == 32 do
    <<1::8, byte_size(principal)::unsigned-32, principal::binary, epoch::unsigned-64,
      nonce::binary-12, tag::binary-16, wrapped_key::binary-32>>
  end

  def encode(%__MODULE__{
        version: 2,
        principal: principal,
        key_ref: key_ref,
        wrapped_key: wrapped_key
      })
      when is_binary(principal) and byte_size(principal) > 0 and
             byte_size(principal) <= 4_294_967_295 and is_binary(key_ref) and
             byte_size(key_ref) > 0 and byte_size(key_ref) <= 4_294_967_295 and
             is_binary(wrapped_key) and byte_size(wrapped_key) > 0 and
             byte_size(wrapped_key) <= 4_294_967_295 do
    <<2::8, byte_size(principal)::unsigned-32, principal::binary,
      byte_size(key_ref)::unsigned-32, key_ref::binary, byte_size(wrapped_key)::unsigned-32,
      wrapped_key::binary>>
  end

  @spec decode(binary()) :: {:ok, t()} | {:error, :bad_envelope}
  def decode(<<1::8, principal_size::unsigned-32, rest::binary>>)
      when principal_size > 0 and byte_size(rest) == principal_size + 68 do
    <<principal::binary-size(principal_size), epoch::unsigned-64, nonce::binary-12,
      tag::binary-16, wrapped_key::binary-32>> = rest

    {:ok,
     %__MODULE__{
       principal: principal,
       epoch: epoch,
       nonce: nonce,
       tag: tag,
       wrapped_key: wrapped_key
     }}
  end

  def decode(<<2::8, principal_size::unsigned-32, rest::binary>>)
      when principal_size > 0 and byte_size(rest) >= principal_size + 8 do
    with <<principal::binary-size(principal_size), key_ref_size::unsigned-32,
           tail::binary>> <- rest,
         true <- key_ref_size > 0 and byte_size(tail) >= key_ref_size + 4,
         <<key_ref::binary-size(key_ref_size), wrapped_size::unsigned-32,
           wrapped_key::binary>> <- tail,
         true <- wrapped_size > 0 and byte_size(wrapped_key) == wrapped_size do
      {:ok,
       %__MODULE__{
         version: 2,
         principal: principal,
         key_ref: key_ref,
         wrapped_key: wrapped_key
       }}
    else
      _ -> {:error, :bad_envelope}
    end
  end

  def decode(_binary), do: {:error, :bad_envelope}
end
