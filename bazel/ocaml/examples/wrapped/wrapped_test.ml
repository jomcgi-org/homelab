(* Two wrapped libraries each ship a module named [Util]; without dune-style
   wrapping these collide at link (two units named Util). With wrapping they
   compile as Colors__Util / Shapes__Util and link side by side.

   [widgets] has no main module, so its public wrapper [Widgets] is generated
   from the members alone. *)
let () =
  assert (Colors.describe () = "color:colors");
  assert (Shapes.describe () = "shape:shapes");
  assert (Widgets.Button.describe () = "button(width=80)");
  print_endline "wrapped: ok"
