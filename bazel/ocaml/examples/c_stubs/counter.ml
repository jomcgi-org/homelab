(* The implementation is a C function (counter_stubs.c), bound via `external`.
   The ocaml_library `c_srcs` attr compiles and folds the stub into counter.a. *)
external popcount : int -> int = "ml_popcount"
