/* C stub backing Intern.hash: FNV-1a over the bytes of an OCaml string, masked
   to 62 bits so the result always fits an OCaml int. Compiled by ocamlopt
   (which supplies the caml runtime headers) and folded into the library archive by
   the ocaml_library rule, so linkers resolve "tc_fnv1a" automatically. This is
   a leaf function that allocates no OCaml values, so it needs no CAMLparam/
   CAMLreturn frame -- same shape as examples/c_stubs/counter_stubs.c. */
#include <stdint.h>
#include <caml/mlvalues.h>

CAMLprim value tc_fnv1a(value s) {
  const unsigned char *p = (const unsigned char *)String_val(s);
  mlsize_t n = caml_string_length(s);
  uint64_t h = 1469598103934665603ULL; /* FNV-1a 64-bit offset basis */
  for (mlsize_t i = 0; i < n; i++) {
    h ^= (uint64_t)p[i];
    h *= 1099511628211ULL; /* FNV-1a 64-bit prime */
  }
  return Val_long((intnat)(h & 0x3FFFFFFFFFFFFFFFULL));
}
