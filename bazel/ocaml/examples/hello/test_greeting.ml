(* An OCaml test following the standard convention (cf. Dune's `(test)` stanza):
   a plain executable that exits 0 on success and raises (non-zero exit) on
   failure. Exercises the local library (Greeting/Message) and links the vendored
   external opam dep (Fmt) to print the result. *)

let check name actual expected =
  if actual <> expected then (
    Fmt.epr "FAIL %s:@.  expected: %S@.  actual:   %S@." name expected actual;
    exit 1)

(* Bazel runs tests with CWD at the runfiles root, so `data` files are
   reachable at their workspace-relative paths. *)
let read_first_line path =
  let ic = open_in path in
  let line = input_line ic in
  close_in ic;
  line

let () =
  check "render Bazel"
    (Greeting.render "Bazel")
    "Hello from the homelab OCaml ruleset \u{2014} greetings, Bazel!";
  check "render World"
    (Greeting.render "World")
    "Hello from the homelab OCaml ruleset \u{2014} greetings, World!";
  check "render Bazel matches runfiles testdata"
    (Greeting.render "Bazel")
    (read_first_line "bazel/ocaml/examples/hello/testdata/expected.txt");
  Fmt.pr "ok: greeting tests passed@."
