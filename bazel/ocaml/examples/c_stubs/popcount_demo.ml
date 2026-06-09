let () =
  List.iter
    (fun n -> Printf.printf "popcount(%d) = %d\n" n (Counter.popcount n))
    [ 0; 1; 255; 1024 ]
