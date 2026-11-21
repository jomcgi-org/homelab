#!/usr/bin/env python3
"""Exercise the installer with an isolated group database and systemctl."""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

INSTALLER = Path(__file__).with_name("install-t3-socket.sh").resolve()

COMMAND = """import os
import sys
from pathlib import Path

command = Path(sys.argv[0]).name
args = sys.argv[1:]
directory = Path(os.environ["T3_INSTALL_TEST_DIR"])
groups = directory / "groups"
entry = "t3-relay:x:65532:"

if command == "id" and args == ["-u"]:
    print(0)
elif command == "hostname" and args == ["-s"]:
    print("node-1")
elif command == "getent" and args == ["passwd"]:
    print((directory / "passwd").read_text(), end="")
elif command == "getent" and args[0] == "group":
    rows = groups.read_text().splitlines()
    if len(args) == 2:
        rows = [row for row in rows if row.split(":")[0] == args[1]]
        if not rows:
            sys.exit(2)
    print("\\n".join(rows))
elif command == "groupadd" and args == ["--system", "--gid", "65532", "t3-relay"]:
    if any(row.split(":")[2] == "65532" for row in groups.read_text().splitlines()):
        sys.exit(4)
    with groups.open("a") as stream:
        stream.write(entry + "\\n")
elif command == "install" and args[:2] == ["-m", "0644"]:
    assert "SocketGroup=t3-relay" in Path(args[2]).read_text().splitlines()
    assert args[3:] == [str(Path(args[2]).with_suffix(".service")), "/etc/systemd/system/"]
elif command == "systemctl" and args == ["daemon-reload"]:
    pass
elif command == "systemctl" and args == ["enable", "--now", "t3-cluster.socket"]:
    assert entry in groups.read_text().splitlines(), "socket enabled before group creation"
    with (directory / "socket-starts").open("a") as stream:
        stream.write("start\\n")
else:
    sys.exit(f"unexpected command: {command} {args}")
"""


def test_socket_installer():
    with TemporaryDirectory(prefix="t3-socket-test-") as tmp:
        directory = Path(tmp)
        commands = directory / "commands"
        commands.mkdir()
        fixture = commands / "fixture"
        fixture.write_text(f"#!{sys.executable}\n{COMMAND}")
        fixture.chmod(0o700)
        for command in ("id", "hostname", "getent", "groupadd", "install", "systemctl"):
            (commands / command).symlink_to(fixture)

        def run(mode, groups="", passwd="", starts=0, succeeds=True):
            (directory / "groups").write_text(groups)
            (directory / "passwd").write_text(passwd)
            (directory / "socket-starts").unlink(missing_ok=True)
            result = subprocess.run(
                ["bash", str(INSTALLER), mode],
                env={
                    **os.environ,
                    "PATH": f"{commands}:{os.environ['PATH']}",
                    "T3_INSTALL_TEST_DIR": tmp,
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert (result.returncode == 0) == succeeds, result.stderr
            log = directory / "socket-starts"
            assert (len(log.read_text().splitlines()) if log.exists() else 0) == starts
            return (directory / "groups").read_text()

        assert run("--stage") == ""
        created = run("--activate", starts=1)
        assert created == "t3-relay:x:65532:\n"
        assert run("--activate", groups=created, starts=1) == created
        active = created + "t3-cluster:x:62939:\n"
        assert run("--activate", groups=active, starts=1) == active
        for groups in (
            "other:x:65532:\n",
            "t3-relay:x:444:\n",
            "t3-relay:x:65532:host-user\n",
            created + "alias:x:65532:\n",
        ):
            assert run("--activate", groups=groups, succeeds=False) == groups
        assert (
            run(
                "--activate",
                passwd="host-user:x:4321:65532::/tmp:/bin/sh\n",
                succeeds=False,
            )
            == ""
        )


if __name__ == "__main__":
    test_socket_installer()
