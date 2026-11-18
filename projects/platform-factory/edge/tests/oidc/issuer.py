"""Ephemeral OIDC token endpoint and kagent echo backend for the kind test."""

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import ssl
import subprocess
import threading
import time
from urllib.parse import parse_qs

ISSUER = "https://issuer.edge.svc.cluster.local/application/o/factory-edge/"


def encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed_token(subject):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "factory-edge",
        "sub": subject,
        "iat": now,
        "exp": now + 600,
        "groups": ["authentik Admins"] if subject == "operator" else ["applicants"],
    }
    header = {"alg": "RS256", "kid": "test"}
    payload = ".".join(encode(json.dumps(part).encode()) for part in [header, claims])
    signature = subprocess.check_output(
        ["openssl", "dgst", "-sha256", "-sign", "/test/key.pem"], input=payload.encode()
    )
    return payload + "." + encode(signature)


class Fixture(BaseHTTPRequestHandler):
    def reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/application/o/factory-edge/jwks/":
            modulus = (
                subprocess.check_output(
                    ["openssl", "rsa", "-in", "/test/key.pem", "-modulus", "-noout"]
                )
                .decode()
                .strip()
                .split("=")[1]
            )
            self.reply(
                {
                    "keys": [
                        {
                            "kid": "test",
                            "kty": "RSA",
                            "alg": "RS256",
                            "e": "AQAB",
                            "n": encode(bytes.fromhex(modulus)),
                        }
                    ]
                }
            )
        else:
            self.reply({"authorization": self.headers.get("Authorization")})

    def do_POST(self):
        assert self.path == "/application/o/token/"
        data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        subject = data["code"][0]
        assert subject in ("operator", "applicant")
        self.reply(
            {
                "token_type": "Bearer",
                "expires_in": 600,
                "access_token": "fixture-access-" + subject,
                "id_token": signed_token(subject),
            }
        )


server = HTTPServer(("", 8443), Fixture)
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain("/test/ca.pem", "/test/key.pem")
server.socket = tls.wrap_socket(server.socket, server_side=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
HTTPServer(("", 8080), Fixture).serve_forever()
