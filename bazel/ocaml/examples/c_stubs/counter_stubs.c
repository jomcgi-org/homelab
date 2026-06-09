/* C stub backing Counter.popcount. Compiled by ocamlopt (which supplies the
   caml/*.h headers) and folded into the library archive by the ocaml_library
   rule, so anything linking :counter resolves "ml_popcount" automatically. */
#include <caml/mlvalues.h>

CAMLprim value ml_popcount(value n) {
  return Val_int(__builtin_popcount((unsigned int)Int_val(n)));
}
