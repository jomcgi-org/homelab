1. In `projects/platform-factory/substrate/resources/patches/postgres.yaml:53`,
   `public.scoped_reaper_actors()` selects the actor's atespace, name and proto
   from the same row. The two-column `public.reaper_actors()` at line 48 keeps
   the previous startup hook compatible with the persistent database during
   rollback. To inspect every actor query in the current reaper, run
   `rg -n 'reaper_actors\(' projects/factory/connector/internal/reaper/reaper.go`.
   The snapshot at line 92 reads all three columns. Lines 71–109 associate
   each row with attempts using the namespace and session actor name together.
   The suspension recheck at line 123 matches both fields and the proto.
   The disappearance recheck at line 168 uses the attempt namespace read at
   line 151 and the session actor name.
2. `TestScanWorkerDisappearance` in
   `projects/factory/connector/internal/reaper/reaper_test.go` supplies the
   same actor name in three atespaces. During the pod lookup it replaces only
   the local assignment and asserts the original fence stays open. With the
   original local assignment restored, it checks that the fence records the
   disappearance. Run `bazel test //projects/factory/connector/internal/reaper:reaper_test`.
