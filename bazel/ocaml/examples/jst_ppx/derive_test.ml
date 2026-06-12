(* Wave B acceptance: [@@deriving compare, sexp_of, hash] end to end.
   open! Base puts compare_int / sexp_of_string / hash_fold_t & friends in
   scope, exactly the upstream convention for these derivers. *)
open! Base

type point =
  { x : int
  ; y : int
  ; label : string
  }
[@@deriving compare, sexp_of, hash]

let () =
  let a = { x = 1; y = 2; label = "a" } in
  let b = { x = 1; y = 2; label = "a" } in
  let c = { x = 3; y = 4; label = "c" } in
  (* compare: equal records are 0, distinct ones are not. *)
  assert (compare_point a b = 0);
  assert (compare_point a c <> 0);
  (* hash: equal values hash equal. *)
  assert (hash_point a = hash_point b);
  (* sexp_of: shape is stable. *)
  let s = Sexp.to_string (sexp_of_point c) in
  assert (String.equal s "((x 3)(y 4)(label c))");
  Stdlib.print_endline "jst ppx e2e ok"
