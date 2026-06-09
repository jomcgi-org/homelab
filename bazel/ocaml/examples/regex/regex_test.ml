(* Native ocaml_test: exits non-zero on the first failed check, so `bazel test`
   reads the exit code as the verdict (Dune `(test)` convention). Exercises the
   `re` library's public API end to end. *)
let check name cond =
  if not cond then (
    Printf.eprintf "FAIL: %s\n" name;
    exit 1)

let () =
  let email = Re.Pcre.re {|(\w+)@(\w+)|} |> Re.compile in
  let g = Re.exec email "ping joe@example today" in
  check "group1" (Re.Group.get g 1 = "joe");
  check "group2" (Re.Group.get g 2 = "example");
  check "no-match" (not (Re.execp email "no at sign here"));

  let digits = Re.Posix.re "[0-9]+" |> Re.compile in
  check "posix-replace" (Re.replace digits ~f:(fun _ -> "#") "a1b22c333" = "a#b#c#");

  let ml = Re.Glob.glob "*.ml" |> Re.compile in
  check "glob-match" (Re.execp ml "main.ml");
  check "glob-reject" (not (Re.execp ml "script.py"));

  print_endline "all re tests passed"
