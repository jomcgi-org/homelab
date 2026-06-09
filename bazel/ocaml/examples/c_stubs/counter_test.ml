(* Native ocaml_test: non-zero exit on the first failed check. Exercises the C
   stub through the OCaml wrapper, so it covers the whole c_srcs path: compile
   the .c, fold it into counter.a, and link it into the test binary. *)
let check name cond =
  if not cond then (
    Printf.eprintf "FAIL: %s\n" name;
    exit 1)

let () =
  check "zero" (Counter.popcount 0 = 0);
  check "one" (Counter.popcount 1 = 1);
  check "0xff" (Counter.popcount 0xff = 8);
  check "0b1010" (Counter.popcount 0b1010 = 2);
  print_endline "all c-stub tests passed"
