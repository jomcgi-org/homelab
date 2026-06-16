(* [%blob "secret.txt"] is replaced by secret.txt's contents at compile time by
   the ppx_blob rewriter. The file reaches the preprocess action through the
   library's preprocess_data attr (dune's (preprocessor_deps (file X))); the
   driver stages it next to the sources so ppx_blob's source-dir-relative
   lookup finds it. If the embedding worked, the literal below equals the
   file's contents. *)
let embedded = [%blob "secret.txt"]

let () =
  assert (embedded = "embedded-at-compile-time\n");
  print_endline "ppx_blob: ok"
