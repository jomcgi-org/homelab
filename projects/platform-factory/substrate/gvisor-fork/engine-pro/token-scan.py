"""Fail when the real token is in a layer, an image or a directory.

    SEMGREP_APP_TOKEN=... python3 -I token-scan.py [--token-env NAME]
        --placeholder VALUE --placeholder-member NAME PATH...

PATH is a tar (plain or gzip), such as layer.tar or the image.tar of `crane mutate
--output`, or a directory. A tar is read member by member, and a member that is
itself a tar or a gzip (the layers of an image) is read the same way, four levels
deep. Every other file is read in 1 MiB chunks, so a 340 MB engine is no problem.

For each file the scan looks for the token as it is, backwards, as hex and as
base64 at each of the three byte offsets, and as URL-safe base64. It prints the
place (archive path, member, offset) and never the token.

The placeholder is allowed in one place only: a member named NAME (for example
semgrep/.tok). Anywhere else it is a failure, because that is a sign of a build
that put it into a file by mistake. A scan that finds the placeholder nowhere
fails too, unless --no-require-placeholder is given: the image must hold it.

Exit codes: 0 clean, 1 found (or the placeholder is misplaced or missing),
2 bad use (no token in the environment, a token under 16 characters, a
placeholder equal to the token).
"""

from __future__ import annotations

import argparse
import base64
import gzip
import os
import sys
import tarfile
from pathlib import Path

CHUNK = 1 << 20
MAX_DEPTH = 4


def variants(secret: bytes) -> dict[str, bytes]:
    """The byte strings that show the secret, by kind. Short pieces of an encoding
    (the first and last characters depend on the neighbours) are cut off."""
    out = {"plain": secret, "reversed": secret[::-1], "hex": secret.hex().encode()}
    out["hex-upper"] = secret.hex().upper().encode()
    for offset in (0, 1, 2):
        drop = {0: 0, 1: 2, 2: 3}[offset]
        for kind, encode in (
            ("base64", base64.b64encode),
            ("base64url", base64.urlsafe_b64encode),
        ):
            text = encode(b"\0" * offset + secret).rstrip(b"=")[drop:-2]
            out[f"{kind}+{offset}"] = text
    return out


class Scan:
    def __init__(self, token: bytes, placeholder: bytes, allowed: str) -> None:
        self.patterns = variants(token)
        self.placeholder = placeholder
        self.allowed = allowed
        self.keep = max(len(p) for p in self.patterns.values()) - 1
        self.keep = max(self.keep, len(placeholder) - 1)
        self.findings: list[str] = []
        self.placeholder_ok = 0

    def stream(self, fileobj, where: str, member: str) -> None:
        """Read a plain file chunk by chunk, with an overlap for matches that
        span two chunks."""
        base = 0
        tail = b""
        seen_here: set[str] = set()
        while True:
            chunk = fileobj.read(CHUNK)
            if not chunk:
                return
            data = tail + chunk
            start = base - len(tail)
            for kind, pattern in self.patterns.items():
                at = data.find(pattern)
                if at >= 0 and kind not in seen_here:
                    seen_here.add(kind)
                    self.findings.append(
                        f"TOKEN ({kind}) in {where}: {member} at byte {start + at}"
                    )
            at = data.find(self.placeholder)
            if at >= 0:
                if member.removeprefix("./").lstrip("/") == self.allowed:
                    self.placeholder_ok += 1
                elif "placeholder" not in seen_here:
                    seen_here.add("placeholder")
                    self.findings.append(
                        f"PLACEHOLDER outside {self.allowed} in {where}: {member} at byte {start + at}"
                    )
            base += len(chunk)
            tail = data[-self.keep :] if self.keep else b""

    def file(self, fileobj, where: str, member: str, depth: int) -> None:
        head = fileobj.read(512)
        is_gzip = head[:2] == b"\x1f\x8b"
        is_tar = head[257:262] == b"ustar"
        fileobj.seek(0)
        if depth < MAX_DEPTH and (is_gzip or is_tar):
            try:
                self.archive(fileobj, f"{where}: {member}", depth + 1)
                return
            except (tarfile.TarError, EOFError, OSError):
                # Not a tar after all (a gzip of one file): read the plain bytes.
                fileobj.seek(0)
                if is_gzip:
                    self.stream(
                        gzip.GzipFile(fileobj=fileobj), where, member + " (gunzipped)"
                    )
                    fileobj.seek(0)
        self.stream(fileobj, where, member)

    def archive(self, fileobj, where: str, depth: int) -> None:
        with tarfile.open(fileobj=fileobj, mode="r:*") as tar:
            for info in tar:
                if not info.isfile():
                    continue
                member = tar.extractfile(info)
                if member is None:
                    continue
                self.file_member(member, where, info.name, depth)

    def file_member(self, member, where: str, name: str, depth: int) -> None:
        # An extracted member is seekable (it is a slice of the tar file).
        self.file(member, where, name, depth)

    def path(self, path: Path) -> None:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    self.path(child)
            return
        with open(path, "rb") as fh:
            self.file(fh, str(path), path.name, 0)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--token-env", default="SEMGREP_APP_TOKEN")
    parser.add_argument("--placeholder", required=True)
    parser.add_argument("--placeholder-member", required=True)
    parser.add_argument("--no-require-placeholder", action="store_true")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env, "")
    if len(token) < 16:
        print(
            f"token-scan: {args.token_env} is empty or under 16 characters",
            file=sys.stderr,
        )
        return 2
    if (
        token == args.placeholder
        or token in args.placeholder
        or args.placeholder in token
    ):
        print("token-scan: the token and the placeholder overlap", file=sys.stderr)
        return 2
    scan = Scan(token.encode(), args.placeholder.encode(), args.placeholder_member)
    for raw in args.paths:
        path = Path(raw)
        if not path.exists():
            print(f"token-scan: {raw} does not exist", file=sys.stderr)
            return 2
        scan.path(path)
    if not scan.placeholder_ok and not args.no_require_placeholder:
        scan.findings.append(
            f"the placeholder is not in {args.placeholder_member} of any scanned archive"
        )
    for line in scan.findings:
        print(line)
    if scan.findings:
        print(f"token-scan: FAIL ({len(scan.findings)} findings)")
        return 1
    print(
        f"token-scan: clean ({len(args.paths)} paths, placeholder in "
        f"{scan.placeholder_ok} place(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
