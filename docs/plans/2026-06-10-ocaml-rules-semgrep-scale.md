# Scaling bazel/ocaml toward Semgrep Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Evolve the `bazel/ocaml` toy ruleset into one that can build Semgrep: ppx, a pinned opam universe, wrapped third-party libraries, codegen stanzas, real C stubs, and dual-arch (linux x86_64 + aarch64) toolchains.

**Architecture:** Per ADR [tooling/004](../decisions/tooling/004-ocaml-rules-for-semgrep.md): scale the custom ruleset (not obazl). The hermetic executor-built compiler design stays; work proceeds in phases, each phase one PR, ordered by the empirical gap inventory (ppx blocks 167/188 Semgrep library stanzas, so the dependency ladder that unblocks ppx comes first).

**Tech Stack:** Starlark rules + POSIX sh driver (`bazel/ocaml/`), Python repo-rule generators (`dune2bazel.py`), OCaml 5.3 (semgrep fork) built on BuildBuddy RBE, ppxlib driver model.

**Verification model:** This repo has **no local test loop**. Every phase ends with: commit, push branch, `gh pr create`, `gh pr checks <n> --watch`, iterate on CI failures via `mcp__buildbuddy__*` tools. Intermediate "verify" steps are code-level checks (`helm`-style template rendering does not apply here; for Starlark use `buildifier` via `format`). Do not run `bazel test` locally.

**Phase = PR.** One comprehensive code review at the end of each PR, per CLAUDE.md cadence. Implementer subagents self-review before each commit.

---

## Phase ordering and rationale

| Phase | PR         | Delivers                                                                       | Why this order                                                                       |
| ----- | ---------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| 1     | hygiene    | Repo cleanliness + small rule fixes                                            | Zero-risk, removes landmines before real work                                        |
| 2     | wrapping   | Dune-style `Lib__Module` wrapping for third-party libs                         | Prerequisite for any two real opam packages to link; prerequisite for ppxlib closure |
| 3     | lockfile   | Pinned opam universe machinery + the ppxlib dependency ladder                  | ppx rule needs ppxlib + ~6 transitive packages built from source                     |
| 4     | ppx        | `ocaml_ppx` rule + `preprocess` support                                        | The dominant Semgrep blocker (167/188 stanzas)                                       |
| 5     | codegen    | `ocamllex`, `menhir`, generic `(rule)` translation                             | 31 stanzas in Semgrep CE; needed before any parser dir builds                        |
| 6     | c-stubs    | `cc_library` delegation, `foreign_archives`, tree-sitter runtime + one grammar | pcre + tree-sitter are load-bearing for Semgrep                                      |
| 7     | multi-arch | Platforms, constraints, BuildBuddy arm64 pool, per-arch sysroots               | Independent of 2-6; scheduled here so perf lessons land first                        |
| 8     | semgrep    | Fetch pinned Semgrep CE, translate `libs/` bottom-up                           | The goal; gated on everything above                                                  |

Phases 1-4 are specified task-by-task below. Phases 5-8 are scoped with files and acceptance criteria, and carry an explicit **re-plan checkpoint**: expand them into bite-sized tasks (fresh planning pass) when reached, because their details depend on what CI teaches us in 2-4.

---

## Phase 1: Hygiene (PR `chore/ocaml-hygiene`)

### Task 1.1: Remove committed build artifacts

**Files:**

- Delete: `bazel/ocaml/examples/regex/*.cmi`, `*.cmx`, `*.o` (6 files, verified committed via `git ls-files`)
- Create: `bazel/ocaml/.gitignore`

**Step 1:** Create the worktree:

```bash
git -C ~/repos/homelab worktree add -b chore/ocaml-hygiene /tmp/claude-worktrees/ocaml-hygiene origin/main
```

**Step 2:** Remove artifacts and add ignore rules:

```bash
cd /tmp/claude-worktrees/ocaml-hygiene
git rm bazel/ocaml/examples/regex/*.cmi bazel/ocaml/examples/regex/*.cmx bazel/ocaml/examples/regex/*.o
```

Create `bazel/ocaml/.gitignore`:

```gitignore
*.cmi
*.cmx
*.cmo
*.cma
*.cmxa
*.o
*.a
```

**Step 3:** Commit:

```bash
git commit -m "chore(ocaml): remove committed compiler artifacts, ignore OCaml outputs"
```

### Task 1.2: Fix the stale toolchain comment in `bazel/ocaml/BUILD`

**Files:**

- Modify: `bazel/ocaml/BUILD:10-12`

**Step 1:** Replace the comment block above `ocaml_toolchain(name = "ocaml_tools", ...)`. It currently claims the compiler comes from "the pinned container image", which contradicts the sysroot design. New comment:

```starlark
# Tool configuration (ocamlfind preference, extra flags). The compiler is the
# hermetic sysroot tar built by //bazel/ocaml/toolchain:ocaml_compiler and
# staged into every action (see toolchain.bzl).
```

**Step 2:** Commit: `git commit -am "docs(ocaml): fix stale container-image comment in BUILD"`

### Task 1.3: Guard the sysroot filegroup assumption

**Files:**

- Modify: `bazel/ocaml/toolchain.bzl:33-44`

**Step 1:** `_ocaml_toolchain_impl` takes `sysroot_files[0]` blindly. Make it select the `.tar` and fail on ambiguity:

```starlark
def _ocaml_toolchain_impl(ctx):
    sysroot_files = ctx.files.sysroot
    tars = [f for f in sysroot_files if f.extension == "tar"]
    if len(tars) != 1:
        fail("ocaml_toolchain: expected exactly one .tar in sysroot, got %s" %
             [f.short_path for f in sysroot_files])
    return [platform_common.ToolchainInfo(
        ocaml = OcamlToolchainInfo(
            sysroot_files = depset(sysroot_files),
            sysroot_tar = tars[0],
            use_ocamlfind = ctx.attr.use_ocamlfind,
            extra_compile_flags = ctx.attr.extra_compile_flags,
        ),
    )]
```

**Step 2:** Commit: `git commit -am "fix(ocaml): select sysroot tar explicitly instead of files[0]"`

### Task 1.4: Add `data` (runfiles) support to binary/test rules

**Files:**

- Modify: `bazel/ocaml/rules.bzl` (`_COMMON_ATTRS`, `_ocaml_binary_impl`)

**Step 1:** Add to `_COMMON_ATTRS`:

```starlark
    "data": attr.label_list(
        allow_files = True,
        doc = "Runtime files made available in the runfiles tree (test corpora etc.).",
    ),
```

**Step 2:** In `_ocaml_binary_impl`, replace the return with:

```starlark
    return [DefaultInfo(
        files = depset([exe]),
        executable = exe,
        runfiles = ctx.runfiles(files = [exe] + ctx.files.data),
    )]
```

**Step 3:** Exercise it: add a `data` file to `examples/hello`'s test. Create `bazel/ocaml/examples/hello/testdata/expected.txt` containing `Hello, World!` and pass `data = ["testdata/expected.txt"]` to the existing `ocaml_test`. Modify `test_greeting.ml` to read it (runfiles-relative path: `Sys.getenv "TEST_SRCDIR"` is not set by plain Bazel sh runner for native exes started directly; use the file's runfiles path relative to CWD, which Bazel sets to the runfiles root for tests):

```ocaml
let expected =
  let ic = open_in "bazel/ocaml/examples/hello/testdata/expected.txt" in
  let line = input_line ic in
  close_in ic;
  line
```

(Keep the existing assertions; add one comparing `Greeting.greet ()` output against `expected`.)

**Step 4:** Commit: `git commit -am "feat(ocaml): data attr for runfiles on ocaml_binary/ocaml_test"`

### Task 1.5: Phase gate: push and watch CI

```bash
git push -u origin chore/ocaml-hygiene
gh pr create --title "chore(ocaml): hygiene fixes ahead of Semgrep scaling" --body "Phase 1 of docs/plans/2026-06-10-ocaml-rules-semgrep-scale.md"
gh pr checks <number> --watch
```

Expected: `//bazel/ocaml/...` targets all green (build_test + 3 ocaml_test). On failure, read the invocation via `mcp__buildbuddy__get_invocation` (commitSha selector), quote the error verbatim, fix, push. Merge with `gh pr merge --auto --rebase`.

---

## Phase 2: Wrapped libraries (PR `feat/ocaml-wrapping`)

Dune's wrapping scheme, which we replicate exactly:

1. Member module `cset.ml` of library `re` is compiled as unit `Re__Cset` (achieved by staging the file as `re__Cset.ml`; ocamlopt derives the unit name from the file name).
2. A generated alias module `re__.ml` contains `module Cset = Re__Cset` lines for every member, compiled first with `-no-alias-deps -w -49` (suppress "unused module" + missing-cmi warnings).
3. Every member compiles with `-open Re__`, so plain references (`Cset.union`) resolve through the alias module.
4. A source file named after the library (`re.ml`) is the main module: compiled last, as plain unit `Re`, also with `-open Re__`.

`ocamldep -sort` must run over the **original** file names (members reference each other by plain name), and the rename happens after sorting.

### Task 2.1: Add `wrapped` + `lib_name` to the rule and provider

**Files:**

- Modify: `bazel/ocaml/rules.bzl`

**Step 1:** Add attrs to `_COMMON_ATTRS`:

```starlark
    "wrapped": attr.bool(
        default = False,
        doc = "Dune-style wrapping: members compile as <Lib>__<Module> behind a " +
              "generated alias module, with -open <Lib>__. Matches dune's default " +
              "(wrapped true). Our first-party default is False (Semgrep house style).",
    ),
```

**Step 2:** In `_driver_args`, thread it through:

```starlark
    args.add("--wrapped", "1" if getattr(ctx.attr, "wrapped", False) else "0")
```

(`ocaml_binary`/`ocaml_test` pass `--wrapped 0`; wrapping is a library concept. Guard with `getattr` so the shared helper stays shared.)

**Step 3:** Commit: `git commit -am "feat(ocaml): wrapped attr plumbed to driver (no behavior yet)"`

### Task 2.2: Implement wrapping in the driver

**Files:**

- Modify: `bazel/ocaml/driver/ocaml_compile.sh`

**Step 1:** Accept `--wrapped` in the arg loop:

```sh
	--wrapped) WRAPPED="$2" && shift 2 ;;
```

(initialize `WRAPPED="0"` with the other defaults).

**Step 2:** After the existing `ocamldep -sort` (which must keep running on original names), add the wrapped staging pass. Replace the compile loop section with:

```sh
# --- Wrapping (dune scheme) --------------------------------------------------
# Members become <lib>__<Module>; a generated alias module <lib>__.ml maps
# plain names; everything compiles with -open <Lib>__. The main module (a
# source named exactly <lib>.ml) keeps its name. ocamldep ran on original
# names above; we rename in sorted order.
capitalize() { _h=$(printf %.1s "$1" | tr '[:lower:]' '[:upper:]'); printf '%s%s' "$_h" "${1#?}"; }

OPENFLAG=""
if [ "$WRAPPED" = "1" ]; then
	ALIAS_MOD="${NAME}__"
	ALIAS_ML="$WORK/$ALIAS_MOD.ml"
	: > "$ALIAS_ML"
	RENAMED_ORDER=""
	for ml in $ORDER; do
		base="${ml%.ml}"
		if [ "$base" = "$NAME" ]; then
			RENAMED_ORDER="$RENAMED_ORDER $ml"
			continue
		fi
		Mod="$(capitalize "$base")"
		mv "$WORK/$ml" "$WORK/${NAME}__$Mod.ml"
		[ -f "$WORK/$base.mli" ] && mv "$WORK/$base.mli" "$WORK/${NAME}__$Mod.mli"
		echo "module $Mod = $(capitalize "${NAME}__$Mod")" >> "$ALIAS_ML"
		RENAMED_ORDER="$RENAMED_ORDER ${NAME}__$Mod.ml"
	done
	ORDER="$RENAMED_ORDER"
	"$OCAMLOPT" $CFLAGS $INCFLAGS -no-alias-deps -w -49 -c "$ALIAS_ML"
	CMX_LIST="$WORK/$ALIAS_MOD.cmx"
	OPENFLAG="-open $(capitalize "$ALIAS_MOD")"
fi

# --- Compile each module (.mli before .ml) ----------------------------------
for ml in $ORDER; do
	base="${ml%.ml}"
	if [ -f "$WORK/$base.mli" ]; then
		"$OCAMLOPT" $CFLAGS $INCFLAGS $OPENFLAG -no-alias-deps -c "$WORK/$base.mli"
	fi
	"$OCAMLOPT" $CFLAGS $INCFLAGS $OPENFLAG -no-alias-deps -c "$WORK/$ml"
	CMX_LIST="$CMX_LIST $WORK/$base.cmx"
done
```

(When `WRAPPED=0`, `OPENFLAG` is empty, `CMX_LIST` starts empty, and `-no-alias-deps` is harmless; behavior is unchanged.)

**Step 3:** Commit: `git commit -am "feat(ocaml): dune-style module wrapping in the compile driver"`

### Task 2.3: The collision test (the case that motivated all of this)

**Files:**

- Create: `bazel/ocaml/examples/wrapped/BUILD`
- Create: `bazel/ocaml/examples/wrapped/colors.ml`, `shapes.ml` (two libs that both ship a `Util` module), `wrapped_test.ml`

**Step 1:** Write the failing case first. Two wrapped libraries each containing a module named `util`:

`examples/wrapped/colors_util.ml` → no. Layout (one dir per lib, srcs must be separate):

- `examples/wrapped/colors/util.ml`: `let tag = "colors"`
- `examples/wrapped/colors/colors.ml`: `let describe () = "color:" ^ Util.tag`
- `examples/wrapped/shapes/util.ml`: `let tag = "shapes"`
- `examples/wrapped/shapes/shapes.ml`: `let describe () = "shape:" ^ Util.tag`
- `examples/wrapped/wrapped_test.ml`:

```ocaml
let () =
  assert (Colors.describe () = "color:colors");
  assert (Shapes.describe () = "shape:shapes");
  print_endline "wrapped: ok"
```

BUILD files: `ocaml_library(name = "colors", srcs = glob(["colors/*.ml"]), wrapped = True)` (same for shapes), `ocaml_test(name = "wrapped_test", srcs = ["wrapped_test.ml"], deps = [":colors", ":shapes"])`.

Without wrapping, this fails at link with two `Util` units; with wrapping it passes. CI is the arbiter (no local run).

**Step 2:** Commit: `git commit -am "test(ocaml): wrapped-library collision example (two Util modules link)"`

### Task 2.4: Teach `dune2bazel.py` the `wrapped` field

**Files:**

- Modify: `bazel/ocaml/opam/dune2bazel.py`

**Step 1:** Move `wrapped` from rejected to modeled. In `gen_library`:

- Add `"wrapped"` to `_SUPPORTED_LIBRARY_FIELDS`.
- Dune default is wrapped; emit `wrapped = True` unless the stanza says `(wrapped false)`:

```python
wrapped_field = _field(stanza, "wrapped")
wrapped = True
if wrapped_field and _atom(wrapped_field[0]) == "false":
    wrapped = False
```

Emit `    wrapped = True,` into the generated target when true.

**Step 2:** Re-pin `re` to a modern release (the 1.11.0 ceiling is documented as lifted) in `opam/packages.bzl`, e.g. 1.13.x: update `url`/`sha256`/`strip_prefix` (fetch the `.tbz` release asset URL and compute sha256 with `curl -sL <url> | shasum -a 256`). `re`'s dune stanza has no `wrapped` field, so it now builds wrapped, exercising the generator path. The README's `Fmt`/`Str` caveat paragraph and `examples/regex` restriction become obsolete: update `examples/regex/BUILD` to also dep on `//bazel/ocaml/third_party/fmt` and use it in `regex_demo.ml` (e.g. print via `Fmt.str "%d"`), proving the collision is gone. Note: the vendored first-party `fmt` stays unwrapped; the collision was re's _internal_ `Fmt`, now namespaced as `Re__Fmt`.

**Step 3:** Update `bazel/ocaml/README.md`: rewrite the "Module-name caveat" section to describe wrapping, and refresh the dune2bazel scope list.

**Step 4:** Commit: `git commit -am "feat(ocaml): translate dune wrapped field; bump re past the 4.11 ceiling"`

### Task 2.5: Phase gate: push, CI, review, merge

Same loop as Task 1.5. PR title: `feat(ocaml): dune-style wrapped libraries`. Watch for two specific failure shapes: alias-module warnings promoted to errors (fix is the `-w -49` flag, already in the driver) and `ocamldep` order issues on the renamed members (the sort-before-rename ordering exists precisely to prevent this; if CI disagrees, quote the error before touching the driver).

---

## Phase 3: Opam lockfile universe (PR `feat/ocaml-opam-lock`)

Goal: replace the hand-grown `OPAM_PACKAGES` list with generated pins, and build the ppxlib dependency ladder from source. Target closure for this phase (bottom-up, each its own pinned package, all dune projects):

```
sexplib0  →  stdlib-shims  →  ocaml-compiler-libs  →  ppxlib  (+ ppx_deriving for Phase 4)
```

(Exact transitive set may differ at the pinned versions; the lock tool resolves it, and the reject-loudly translator names anything missing.)

### Task 3.1: Lock format + generator script

**Files:**

- Create: `bazel/ocaml/opam/lock.json` (committed, human-readable pin set)
- Create: `bazel/ocaml/opam/update_lock.py` (manual tool, not a build action)
- Modify: `bazel/ocaml/opam/packages.bzl` (becomes a thin loader of lock.json)

**Step 1:** Define `lock.json`:

```json
{
  "packages": [
    {
      "name": "re",
      "version": "1.13.2",
      "repo": "ocaml_re",
      "url": "https://github.com/ocaml/ocaml-re/releases/download/1.13.2/re-1.13.2.tbz",
      "sha256": "<sha256>",
      "strip_prefix": "re-1.13.2",
      "src_dir": "lib"
    }
  ]
}
```

**Step 2:** `update_lock.py`: given `name==version` pins, fetch `https://raw.githubusercontent.com/ocaml/opam-repository/master/packages/<name>/<name>.<version>/opam`, parse the `url { src: ... checksum: ... }` section (prefer sha256; sha512-only entries get the tarball fetched and sha256 computed locally), and rewrite `lock.json`. This runs manually on a workstation (network), never in the build. Include `--add name==version` and `--verify` (re-download and re-hash everything) modes.

**Step 3:** `packages.bzl` becomes:

```starlark
def _load_lock():
    # json.decode over the lock file, exposed to the module extension.
    ...
```

Note: repository rules/extensions can read files via `mctx.read(Label("//bazel/ocaml/opam:lock.json"))` + `json.decode`. Wire `extension.bzl` to that instead of the Starlark list. `src_dir` stays per-package metadata in the lock (it is dune-project layout, not opam metadata; `update_lock.py` preserves hand-set values on regeneration).

**Step 4:** Commit: `git commit -am "feat(ocaml): json lockfile + update_lock.py for pinned opam universe"`

### Task 3.2: dune2bazel: multi-stanza and inter-package deps

**Files:**

- Modify: `bazel/ocaml/opam/dune2bazel.py`
- Modify: `bazel/ocaml/opam/extension.bzl`

**Step 1:** Lift the "exactly one (library) stanza" restriction: emit one `ocaml_library` per `(library)` stanza, skipping non-library stanzas it can _prove_ inert (`env`, `dirs`, docs `rule`s are not provable; keep rejecting `rule` until Phase 5). Multiple dune files per package: extend the repo rule to accept a list of `src_dir`s.

**Step 2:** Resolve `(libraries x)` beyond stdlib: a name that matches another locked package (or a `lib_map` entry in lock.json mapping findlib name → Bazel label, e.g. `ppxlib.ast` → `@ocaml_ppxlib//:ast`) emits a `deps = ["@ocaml_x//:x"]` entry instead of `sys.exit`. Unknown names still reject loudly.

**Step 3:** Commit: `git commit -am "feat(ocaml): dune2bazel resolves inter-package deps and multiple stanzas"`

### Task 3.3: Climb the ladder

Add `sexplib0`, `stdlib-shims`, `ocaml-compiler-libs`, `ppxlib`, `ppx_deriving` to the lock (exact versions chosen by what Semgrep CE's opam file accepts at pin time). For each, a `build_test` target in `bazel/ocaml/examples/opam_ladder/BUILD`:

```starlark
load("@bazel_skylib//rules:build_test.bzl", "build_test")

build_test(
    name = "ladder_builds",
    targets = [
        "@ocaml_sexplib0//:sexplib0",
        "@ocaml_ppxlib//:ppxlib",
        ...
    ],
)
```

Expect iteration here: each package will surface a dune field the translator rejects (e.g. ppxlib uses `(modules ...)` submodule structure and multiple sub-libraries). Handle each by extending the translator (preferred) or, for genuinely package-specific weirdness, an override BUILD: `bazel/ocaml/opam/overrides/<name>/BUILD.tpl` that the repo rule installs verbatim when present. Every override is a documented TODO, not a dead end.

`ocaml-compiler-libs` is a special case worth knowing in advance: it is a _re-packaging_ of the compiler's own `compiler-libs` (which our from-source sysroot already ships). Its dune files mostly wrap existing archives; the override mechanism may be the honest answer, linking against the sysroot's `compiler-libs/ocamlcommon.cmxa` via `opam_deps = ["compiler-libs.common"]` with the driver mapping that name to the sysroot subdir `-I +compiler-libs ocamlcommon.cmxa`.

Commit per package: `feat(ocaml): build <pkg> from lockfile`.

### Task 3.4: Phase gate

Push, PR (`feat(ocaml): pinned opam lockfile + ppxlib ladder`), watch CI, review, merge. Acceptance: `ladder_builds` green on RBE.

---

## Phase 4: ppx (PR `feat/ocaml-ppx`)

### Task 4.1: `ocaml_ppx` rule (driver executable in exec config)

**Files:**

- Modify: `bazel/ocaml/rules.bzl`
- Modify: `bazel/ocaml/driver/ocaml_compile.sh` (new mode `ppx-driver`)

**Step 1:** The ppxlib standalone driver is an OCaml program whose entire source is:

```ocaml
let () = Ppxlib.Driver.standalone ()
```

`ocaml_ppx` generates that main, links it against the rewriter libraries (its `deps`), and exposes the executable:

```starlark
def _ocaml_ppx_impl(ctx):
    tc = ctx.toolchains[_TOOLCHAIN_TYPE].ocaml
    dep = _collect(ctx)
    main = ctx.actions.declare_file(ctx.label.name + "_driver_main.ml")
    ctx.actions.write(main, "let () = Ppxlib.Driver.standalone ()\n")
    exe = ctx.actions.declare_file(ctx.label.name)
    args = _driver_args(ctx, tc, "binary", dep.includes.to_list(), dep.opam.to_list(), [main], [])
    args.add("--exe-out", exe.path)
    for c in dep.cmxa.to_list():
        args.add("--cmxa", c.path)
    ctx.actions.run(
        executable = ctx.executable._driver,
        arguments = [args],
        inputs = depset([main], transitive = [dep.includes, dep.cmxa, dep.a, tc.sysroot_files]),
        outputs = [exe],
        mnemonic = "OcamlPpxDriver",
        progress_message = "Linking ppx driver %{label}",
    )
    return [DefaultInfo(files = depset([exe]), executable = exe)]

ocaml_ppx = rule(
    implementation = _ocaml_ppx_impl,
    attrs = _COMMON_ATTRS_NO_SRCS,  # deps/opam_deps/_driver; no srcs (main is generated)
    toolchains = [_TOOLCHAIN_TYPE],
    executable = True,
    doc = "Links a ppxlib standalone driver from rewriter libraries.",
)
```

(`_COMMON_ATTRS_NO_SRCS` is `_COMMON_ATTRS` minus mandatory `srcs`; factor the dict.) Reuses `mode=binary` in the compile driver, so no driver change needed for this task. Export from `defs.bzl`.

**Important subtlety:** the ppx driver runs at _build time_, so it must be built for the **exec** platform. Single-platform today, so this is latent until Phase 7; mark the consuming attr `cfg = "exec"` now (next task) so it is correct from day one.

**Step 2:** Commit: `git commit -am "feat(ocaml): ocaml_ppx rule links a ppxlib standalone driver"`

### Task 4.2: `preprocess` on libraries/binaries

**Files:**

- Modify: `bazel/ocaml/rules.bzl`, `bazel/ocaml/driver/ocaml_compile.sh`

**Step 1:** New attrs:

```starlark
    "preprocess": attr.label(
        executable = True,
        cfg = "exec",
        doc = "An ocaml_ppx driver run over every source before compilation.",
    ),
    "preprocess_runtime_deps": attr.label_list(
        providers = [OcamlInfo],
        doc = "Runtime libraries the rewriter's output requires (e.g. ppx_deriving.runtime); merged into deps.",
    ),
```

Thread into the action: `args.add("--ppx", ctx.executable.preprocess.path)` when set, add the executable to action `inputs` (`ctx.executable.preprocess`), and merge `preprocess_runtime_deps` into `_collect`'s dep walk.

**Step 2:** Driver: accept `--ppx PATH`; after staging sources into `$WORK` and **before** `ocamldep -sort` (derivers can introduce module references), rewrite in place:

```sh
if [ -n "$PPX" ]; then
	for f in "$WORK"/*.ml; do
		"$PPX" --impl "$f" -o "$f.pp" && mv "$f.pp" "$f"
	done
	for f in "$WORK"/*.mli; do
		[ -f "$f" ] || continue
		"$PPX" --intf "$f" -o "$f.pp" && mv "$f.pp" "$f"
	done
fi
```

(ppx output is source-compatible OCaml; in-place keeps file/unit names stable so wrapping and ocamldep are untouched downstream. Resolve `$PPX` to an absolute path the same way as the sysroot tar, since the loop cds.)

**Step 3:** Commit: `git commit -am "feat(ocaml): preprocess attr runs ppx driver over staged sources"`

### Task 4.3: End-to-end: `ppx_deriving.show`

**Files:**

- Create: `bazel/ocaml/examples/ppx/BUILD`, `point.ml`, `ppx_test.ml`

**Step 1:** The 159-use case from the inventory:

`point.ml`:

```ocaml
type point = { x : int; y : int } [@@deriving show]
```

`ppx_test.ml`:

```ocaml
let () =
  let p = Point.{ x = 1; y = 2 } in
  assert (Point.show_point p = "{ Point.x = 1; y = 2 }");
  print_endline "ppx: ok"
```

(Exact `show` formatting may differ by ppx_deriving version; let CI print the actual string on first failure, then pin the assertion. Quote-before-hypothesize applies to our own tests too.)

`BUILD`:

```starlark
load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_ppx", "ocaml_test")

ocaml_ppx(
    name = "show_driver",
    deps = ["@ocaml_ppx_deriving//:ppx_deriving_show", "@ocaml_ppxlib//:ppxlib"],
)

ocaml_library(
    name = "point",
    srcs = ["point.ml"],
    preprocess = ":show_driver",
    preprocess_runtime_deps = ["@ocaml_ppx_deriving//:runtime"],
)

ocaml_test(name = "ppx_test", srcs = ["ppx_test.ml"], deps = [":point"])
```

(Exact labels inside `@ocaml_ppx_deriving` depend on how Task 3.3 translated its dune tree; adjust to what the generated BUILD exposes.)

**Step 2:** Update README: new "ppx" section describing the driver model, plus `dune2bazel` mapping `(preprocess (pps a b))` → a generated `ocaml_ppx` target named `<lib>_ppx` with those rewriter deps, and `(kind ppx_rewriter|ppx_deriver)` → library consumable by `ocaml_ppx`. Implement that mapping in `dune2bazel.py` (this is what unblocks 167 Semgrep stanzas at translate time).

**Step 3:** Commit: `git commit -am "feat(ocaml): ppx_deriving.show end-to-end; translate preprocess/kind"`

### Task 4.4: Phase gate

Push, PR (`feat(ocaml): ppx driver model`), CI, review, merge. Acceptance: `examples/ppx:ppx_test` green; dune2bazel re-run over Semgrep CE (manually, as in the ADR inventory) shows `preprocess`/`wrapped`/`kind` no longer in the rejection tally.

---

## Phase 5: Codegen stanzas: SCOPED, EXPAND AT CHECKPOINT

**Re-plan checkpoint: expand into bite-sized tasks (fresh writing-plans pass) before starting.**

Scope and shape:

- **ocamllex** (10 stanzas): the tool ships in our sysroot (`bin/ocamllex`). New driver step: `.mll` in `srcs` → `ocamllex` → `.ml`, then the normal pipeline. Translator maps `(ocamllex x)`.
- **menhir** (9 stanzas): menhir is an opam package and itself a dune project that _bootstraps with its own stanzas_. Investigate building it via the lock ladder; if its bootstrap resists translation, the documented fallback is pinning a menhir release tarball that ships pre-generated parsers, or an override BUILD. Then `(menhir (modules x))` → run `menhir` (exec tool) producing `x.ml`/`x.mli`.
- **generic `(rule)`** (12 stanzas): translate only the constrained shape Semgrep uses (inspect each at checkpoint); map to `genrule`-equivalent shell in a new driver mode or a plain `genrule` where inputs/outputs are explicit. Reject anything with `(action (run ...))` targets we cannot prove hermetic.
- `inline_tests` (5 stanzas) ride on ppx (`ppx_inline_test`) + a test-runner main; decide at checkpoint whether to support or to patch those five dune files in the Semgrep overlay instead.

Acceptance: the CE inventory rerun shows `rule`/`ocamllex`/`menhir` translate or are explicitly overridden; an `examples/lexparse` calculator (ocamllex + menhir) test is green.

## Phase 6: C stubs via cc_library: SCOPED, EXPAND AT CHECKPOINT

**Re-plan checkpoint before starting.**

- Replace `c_srcs` flat compilation with delegation: `ocaml_library` gains `cc_deps` (providers `CcInfo`); the driver links `libfoo.a` archives staged from `CcInfo` linking contexts. Keep `c_srcs` for trivial single-file stubs (it works and murmur3 fits it) but route headers/include paths through a real `cc_library`.
- `dune2bazel`: `(foreign_stubs (language c) (names ...))` → generated `cc_library` + `cc_deps` edge; `(c_library_flags ...)` → linkopts.
- Ladder targets: `pcre` (the opam package wraps libpcre; needs `conf-libpcre`, i.e. a system lib decision: vendor pcre source as a `cc_library` from a pinned tarball, consistent with hermeticity), then `tree-sitter` runtime (pure C, clean `cc_library`), then one grammar package (`tree-sitter-lang.go` or similar: generated `parser.c` + OCaml bindings).
- Acceptance: an example test matching a regex via the OCaml `pcre` bindings, and one parsing a Go snippet via tree-sitter, both green on RBE.

## Phase 7: Multi-arch toolchains: SCOPED, EXPAND AT CHECKPOINT

**Re-plan checkpoint before starting.** Independent of Phases 2-6; can be reordered earlier if arm64 demand arrives sooner.

- `bazel/platforms/BUILD`: `platform(name = "linux_arm64", constraint_values = ["@platforms//os:linux", "@platforms//cpu:aarch64"], exec_properties = {"Arch": "arm64", ...})` (verify the exact BuildBuddy property names against their executor docs at implementation time; verify the org has an arm64 pool enabled before anything else: a trivial `genrule` printing `uname -m` with that exec property is the probe).
- Two `ocaml_toolchain` instances with `exec_compatible_with`/`target_compatible_with`; the `ocaml_compiler` action inherits the execution platform, so each arch builds its own sysroot on its own pool by construction. The sysroot tar name must become config-dependent (it already is: it is a per-configuration action output).
- ppx drivers and codegen tools already carry `cfg = "exec"` from Phase 4; cross-config correctness falls out of toolchain resolution.
- Acceptance: `//bazel/ocaml/examples/...` green on both platforms in one CI run (`--platforms` split via a small test matrix in `buildbuddy.yaml`), including the ppx example (exec-config driver on arm64 targeting arm64).
- Perf item rides along: prune the sysroot tar (drop `man/`, docs, bytecode artifacts not needed) and zstd it; measure action setup time in BuildBuddy timing profile before deciding on persistent workers.

## Phase 8: Semgrep entry: SCOPED, EXPAND AT CHECKPOINT

**Re-plan checkpoint before starting; this phase gets its own plan document.**

- Pin a Semgrep CE commit as `@semgrep_src` (module extension, same shape as `@ocaml_source`).
- Run the translator over `libs/` bottom-up (`commons` first: it is the root of the internal dep graph, 188 stanzas reference it directly or transitively). Each newly required opam package goes through the Phase 3 lock ladder; each newly rejected dune field gets a named decision (support, override, or upstream patch).
- Success metric for the phase: `libs/commons` + `libs/glob` + one tree-sitter language end-to-end; `semgrep-core` binary is the headline goal after that.
- Semgrep **Pro** follows only after CE: same ruleset, private source repo, expect a small delta of additional dune features (the translator will name them).

---

## Standing rules for every phase

- **Conventional Commits**, one logical change per commit; implementer self-review before each commit.
- **Push to test**: no local `bazel test`. `gh pr checks --watch`, then `mcp__buildbuddy__get_invocation` → `get_target` → `get_log` on red. Quote errors verbatim before hypothesizing; never blame infra without a ruled-out test failure.
- **Reject loudly** stays the translator's contract: silent mistranslation is the only unacceptable failure mode.
- **README discipline**: every phase that changes semantics updates `bazel/ocaml/README.md` in the same PR.
- One comprehensive code review per PR, at the end (CLAUDE.md cadence).
