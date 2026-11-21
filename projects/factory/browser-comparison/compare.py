"""Run from Linux with CHROMIUM set to an absolute browser executable."""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, HTTPServer
from threading import Thread

ROOT = Path(__file__).resolve().parent
BIN = ROOT / "node_modules/.bin"
FIXTURE = ROOT / "fixture.html"


def mcp(url):
    server = subprocess.Popen(
        [BIN / "playwright-mcp", "--config", "config.json", "--isolated"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    serial = 0

    def call(method, params, notify=False):
        nonlocal serial
        serial += 1
        message = dict(jsonrpc="2.0", method=method, params=params)
        if not notify:
            message["id"] = serial
        server.stdin.write(json.dumps(message) + "\n")
        server.stdin.flush()
        if not notify:
            for line in server.stdout:
                response = json.loads(line)
                if response.get("id") == serial:
                    result = response["result"]
                    if result.get("isError"):
                        raise RuntimeError(result["content"])
                    return result
            raise RuntimeError("MCP closed before responding")

    call(
        "initialize",
        dict(
            protocolVersion="2025-03-26",
            capabilities={},
            clientInfo=dict(name="fixture", version="1"),
        ),
    )
    call("notifications/initialized", {}, True)

    def tool(name, args):
        return call("tools/call", dict(name=name, arguments=args))

    tool("browser_navigate", dict(url=url))
    snapshot = tool("browser_snapshot", {})
    assert "Browser evidence fixture" in json.dumps(snapshot)
    tool("browser_take_screenshot", dict(type="png", filename="screen.png"))
    return server


def cli(url):
    def run(*args):
        result = subprocess.check_output([BIN / "playwright-cli", *args], text=True)
        if "### Error" in result:
            raise RuntimeError(result)

    run("open", url, "--config=config.json")
    run("snapshot", "--filename=snapshot.md")
    assert "Browser evidence fixture" in Path("snapshot.md").read_text()
    run("screenshot", "--filename=screen.png")


def cleanup():
    # Linux reparents detached CLI daemons and Chromium grandchildren to us.
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    deadline = time.monotonic() + 10
    while children.read_text().strip():
        for pid in map(int, children.read_text().split()):
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, os.WNOHANG)
            except (ProcessLookupError, ChildProcessError):
                pass
        if time.monotonic() >= deadline:
            raise RuntimeError("descendants survived cleanup")
        time.sleep(0.02)


class Interrupted(Exception):
    pass


def interrupt(signum, frame):
    raise Interrupted(signum)


def trial(arm, mode, browser, url):
    with tempfile.TemporaryDirectory(dir="/tmp") as work:
        os.chdir(work)
        os.environ.update(HOME=work, XDG_CACHE_HOME=work, TMPDIR=work)
        config = dict(
            browser=dict(
                browserName="chromium",
                launchOptions=dict(executablePath=browser, headless=True),
                contextOptions=dict(viewport=dict(width=640, height=480)),
            )
        )
        Path("config.json").write_text(json.dumps(config))
        signal.alarm(45)
        try:
            server = arm(url)
            subprocess.run(["node", ROOT / "validate.cjs"], check=True)
            digest = hashlib.sha256(Path("screen.png").read_bytes()).hexdigest()
            if mode == "cancel":
                signal.raise_signal(signal.SIGINT)
            if mode == "timeout":
                signal.alarm(1)
                signal.pause()
            if server:
                server.stdin.close()
                server.wait(timeout=5)
            else:
                subprocess.run(
                    [BIN / "playwright-cli", "close"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
        except Interrupted as exc:
            if (
                exc.args
                != ({"cancel": signal.SIGINT, "timeout": signal.SIGALRM}.get(mode),)
                or "digest" not in locals()
            ):
                raise
        finally:
            signal.alarm(0)
            cleanup()
            os.chdir(ROOT)
        return dict(
            interface=arm.__name__,
            exit=mode,
            snapshot=True,
            png_sha256=digest,
            remaining_processes=0,
        )


def main():
    browser = os.environ["CHROMIUM"]
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGALRM, interrupt)
    version = subprocess.check_output([browser, "--version"], text=True).strip()
    with tempfile.TemporaryDirectory() as public:
        (Path(public) / FIXTURE.name).write_bytes(FIXTURE.read_bytes())
        with HTTPServer(
            ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=public)
        ) as http:
            Thread(target=http.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{http.server_port}/{FIXTURE.name}"
            results = [
                trial(arm, mode, browser, url)
                for arm in (cli, mcp)
                for mode in ("normal", "cancel", "timeout")
            ]
    print(
        json.dumps(
            dict(
                browser=version,
                fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                trials=results,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
