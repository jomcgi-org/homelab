from pathlib import Path

_DIR = Path(__file__).parent

# workload -> list of (on-disk filename, name, optional virtual path for semgrep)
_SEMGREP = [
    ("python.sample", "python", "corpus/app_python.py"),
    ("golang.sample", "golang", "corpus/app_go.go"),
    ("javascript.sample", "javascript", "corpus/app_ts.ts"),
    ("kubernetes.sample", "kubernetes", "corpus/manifest.yaml"),
    ("rust.sample", "rust", "corpus/app_rs.rs"),
]


def load_corpus(workload: str) -> list[dict]:
    """Return corpus entries for a workload.

    semgrep entries: {name, path, content} (path is the virtual scan path,
    whose extension selects the language pack). sandbox entries: {name, content}.
    """
    if workload == "semgrep":
        base = _DIR / "semgrep"
        return [
            {"name": name, "path": vpath, "content": (base / fname).read_text()}
            for fname, name, vpath in _SEMGREP
        ]
    if workload == "sandbox":
        base = _DIR / "sandbox"
        return sorted(
            ({"name": p.stem, "content": p.read_text()} for p in base.glob("*.sample")),
            key=lambda e: e["name"],
        )
    raise ValueError(f"unknown workload: {workload}")
