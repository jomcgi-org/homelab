"""Loopback-only CPU synthesis sidecar. No storage, credentials or network clients."""

import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

MODEL = "espeak-ng-1.52.0-r0:en-gb:v1"
MAX_TEXT = 2000
MAX_AUDIO = 2 * 1024 * 1024
TIMEOUT = 8
VOICES = ("en-gb", "en-gb+m1", "en-gb+f2", "en-gb+m3", "en-gb+f4")
SLOT = threading.BoundedSemaphore(1)


def synthesize(text, voice, rate, pitch):
    """Plain stdin only: never interpret markup, paths or client voice names."""
    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "clip.wav")
        subprocess.run(
            [
                "/usr/bin/espeak-ng",
                "--stdin",
                "-v",
                voice,
                "-s",
                str(round(175 * rate)),
                "-p",
                str(round(50 * pitch)),
                "-w",
                output,
            ],
            input=text.encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
        )
        with open(output, "rb") as stream:
            audio = stream.read(MAX_AUDIO + 1)
        if len(audio) > MAX_AUDIO or not audio.startswith(b"RIFF"):
            raise ValueError("invalid audio")
        return audio


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Never log campaign text or audio.

    def do_POST(self):
        if self.path != "/synthesize":
            self.send_error(404)
            return
        if not SLOT.acquire(blocking=False):
            self.send_error(429)
            return
        try:
            self.connection.settimeout(2)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16000:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length))
            if set(body) != {"text", "voice", "rate", "pitch", "model"}:
                raise ValueError("invalid fields")
            text = body["text"]
            if not isinstance(text, str) or not 0 < len(text) <= MAX_TEXT:
                raise ValueError("invalid text")
            if body["voice"] not in VOICES or body["model"] != MODEL:
                raise ValueError("invalid voice")
            if not 0.5 <= body["rate"] <= 2 or not 0 <= body["pitch"] <= 2:
                raise ValueError("invalid parameters")
            audio = synthesize(text, body["voice"], body["rate"], body["pitch"])
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
        except (ValueError, KeyError, TypeError):
            self.send_error(422)
        except (OSError, subprocess.SubprocessError):
            self.send_error(503)
        finally:
            SLOT.release()


if __name__ == "__main__":
    # A single HTTP thread bounds even malformed/slow requests. One worker per pod.
    from http.server import HTTPServer

    HTTPServer(("127.0.0.1", 8094), Handler).serve_forever()
