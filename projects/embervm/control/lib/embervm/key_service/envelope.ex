defmodule Embervm.KeyService.Envelope do
  @moduledoc """
  Versioned envelope for a 32-byte data key wrapped by `Embervm.KeyService`.

  The binary form records the principal and KEK epoch alongside the AES-GCM
  nonce, authentication tag, and wrapped key. The principal and epoch are also
  authenticated as AAD, so changing either field makes unwrap fail.
  """

  @enforce_keys [:principal, :epoch, :nonce, :tag, :wrapped_key]
  defstruct version: 1,
            principal: nil,
            epoch: nil,
            nonce: nil,
            tag: nil,
            wrapped_key: nil

  @type t :: %__MODULE__{
          version: 1,
          principal: String.t(),
          epoch: non_neg_integer(),
          nonce: <<_::96>>,
          tag: <<_::128>>,
          wrapped_key: <<_::256>>
        }

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

  def decode(_binary), do: {:error, :bad_envelope}
end
