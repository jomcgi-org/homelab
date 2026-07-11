import os
from flask import Flask, request

app = Flask(__name__)


@app.route("/run")
def run():
    cmd = request.args.get("cmd", "")
    os.system(cmd)  # webhook proof: command injection (should be flagged)
    return "ok"
