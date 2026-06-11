let () =
  let p = Point.{ x = 1; y = 2 } in
  assert (Point.show_point p = "{ Point.x = 1; y = 2 }");
  print_endline "ppx: ok"
