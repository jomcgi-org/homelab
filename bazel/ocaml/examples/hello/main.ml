(* Entry point. Depends on Greeting (sibling lib), Fmt (vendored opam dep,
   pulled in as a Bazel dep) and Unix (an opam_deps findlib package). *)
let () =
  let line = Greeting.render "Bazel" in
  Fmt.pr "%s@." line;
  Fmt.pr "pid=%d@." (Unix.getpid ())
