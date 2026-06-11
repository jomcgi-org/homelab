let () =
  let p = Point_t.{ x = 3; y = 4; label = "p" } in
  let s = Point_j.string_of_point p in
  let p2 = Point_j.point_of_string s in
  assert (p2.Point_t.x = 3 && p2.Point_t.y = 4 && p2.Point_t.label = "p");
  assert (s = {|{"x":3,"y":4,"label":"p"}|});
  print_endline "atdgen: ok"
