# Claude Code late-bound resume patch

This is a local, version-pinned experiment for Claude Code `2.1.220`. It makes
`--input-format stream-json` use the first incoming `user.session_id` as the
resume ID when `--resume` and `--continue` were not supplied. The original
frame is put back into the stream so normal processing still sees it.

The patch modifies the embedded `cli.js` in place and preserves the native
binary's offsets and size. It is not intended for redistribution; do not
replace the installed Claude Code binary until the smoke test passes.

## Rebuild

The canonical production path is Bazel. It downloads the pinned artifact,
patches it, and supplies the patched tar layer to the EmberVM image:

```sh
bazel build //projects/embervm/runtimes/claude:claude_code_patched_tar_amd64
bazel test //projects/embervm/runtimes/claude:claude_code_late_resume_smoke_test
```

The smoke test runs on Linux, where the pinned ELF can execute. It feeds a
deliberately nonexistent session ID, verifies that the ID reaches resume
lookup, and asserts that no model turn or API cost occurs. On macOS the test is
skipped as incompatible, but the patched artifact still builds.

Keep an untouched copy of the downloaded binary, then run:

```sh
python3 tools/claude-code-patch/patch_claude.py \
  /private/tmp/claude-2.1.220.original.exe \
  /private/tmp/claude-2.1.220.late-resume.exe
chmod +x /private/tmp/claude-2.1.220.late-resume.exe
codesign --force --sign - /private/tmp/claude-2.1.220.late-resume.exe
```

The script refuses binaries without the exact `2.1.220` marker. Recreate the
original copy whenever Claude Code upgrades, then re-audit the embedded source
anchor before changing the version gate.

## No-cost smoke test

This exercises resume lookup with a deliberately nonexistent session and makes
zero model turns:

```sh
printf '%s\n' '{"type":"user","session_id":"00000000-0000-0000-0000-000000000000","message":{"role":"user","content":"smoke test"}}' \
  | /private/tmp/claude-2.1.220.late-resume.exe -p \
      --input-format stream-json --output-format stream-json \
      --verbose --max-turns 1
```

Expected output includes `No conversation found with session ID:` for the
deliberately nonexistent ID and a result with `total_cost_usd:0`.
