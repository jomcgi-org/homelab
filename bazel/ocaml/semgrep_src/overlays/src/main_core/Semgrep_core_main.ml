(* Native CE engine entry point for the Bazel source closure. *)

let usage () =
  Printf.eprintf "usage: %s RULES_YAML TARGETS_JSON\n" Sys.argv.(0);
  Stdlib.exit 2

let () =
  if Array.length Sys.argv <> 3 then usage ();
  let config =
    {
      Core_scan_config.default with
      rule_source = Core_scan_config.Rule_files [ Fpath.v Sys.argv.(1) ];
      target_source = Core_scan_config.Target_file (Fpath.v Sys.argv.(2));
      output_format = Core_scan_config.NoOutput;
      num_jobs = Core_scan_config.Force 1;
    }
  in
  match Core_scan.scan config with
  | Error exn ->
      Printf.eprintf "semgrep-core: %s\n" (Exception.to_string exn);
      Stdlib.exit 2
  | Ok result ->
      Printf.printf "matches=%d errors=%d\n"
        (List.length result.processed_matches)
        (List.length result.errors)
