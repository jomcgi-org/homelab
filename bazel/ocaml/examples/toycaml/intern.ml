(* The implementation is a C function (intern_stubs.c), bound via [external].
   The ocaml_library c_srcs attr compiles the stub and folds it into the library
   archive, so anything linking this library resolves "tc_fnv1a" with no extra
   wiring (the same path examples/c_stubs exercises). *)
external fnv1a : string -> int = "tc_fnv1a"

let hash s = fnv1a s
