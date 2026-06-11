(* Exercises the pcre2-ocaml bindings linked against the vendored PCRE2 C
   library through cc_deps: a Perl-style match and a replace. *)
let () =
  let rex = Pcre2.regexp "h(e+)llo" in
  assert (Pcre2.pmatch ~rex "say heeello there");
  assert (not (Pcre2.pmatch ~rex "say hi there"));
  assert (Pcre2.replace ~rex ~templ:"HI" "heeello world" = "HI world");
  print_endline "pcre2: ok"
