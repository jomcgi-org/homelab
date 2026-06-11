/* OCaml C stub binding the cc_library's adder_add. The driver passes the
   cc_deps header dir via -ccopt -I so this #include resolves, and links
   libadder.a into the final binary. */
#include <caml/mlvalues.h>
#include "adder.h"

CAMLprim value ml_adder_add(value a, value b) {
  return Val_int(adder_add(Int_val(a), Int_val(b)));
}
