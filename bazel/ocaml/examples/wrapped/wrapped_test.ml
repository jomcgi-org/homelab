(* Two wrapped libraries each ship a module named [Util]; without dune-style
   wrapping these collide at link (two units named Util). With wrapping they
   compile as Colors__Util / Shapes__Util and link side by side. *)
let () =
  assert (Colors.describe () = "color:colors");
  assert (Shapes.describe () = "shape:shapes");
  print_endline "wrapped: ok"
