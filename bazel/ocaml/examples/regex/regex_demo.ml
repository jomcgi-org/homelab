(* Demonstrates the `re` opam library (fetched from source, built by our own
   ocaml_library from its dune metadata) through its public Re.* API: Perl-style
   capture groups, POSIX classes with replacement, and shell globbing.

   Also links the vendored `fmt` alongside `re`: re ships an internal Fmt
   module, which used to collide with fmt's at link. Wrapping namespaces it as
   Re__Fmt, so both libraries coexist -- the Fmt.str line is the proof. *)
let () =
  let email = Re.Pcre.re {|(\w+)@(\w+)|} |> Re.compile in
  let g = Re.exec email "ping joe@example today" in
  print_endline (Fmt.str "user=%s host=%s" (Re.Group.get g 1) (Re.Group.get g 2));

  let digits = Re.Posix.re "[0-9]+" |> Re.compile in
  let masked = Re.replace digits ~f:(fun _ -> "#") "a1b22c333" in
  Printf.printf "masked=%s\n" masked;

  let ml = Re.Glob.glob "*.ml" |> Re.compile in
  Printf.printf "glob *.ml matches main.ml: %b\n" (Re.execp ml "main.ml");
  print_endline "re demo OK"
