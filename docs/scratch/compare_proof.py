import os
from flask import Flask, request

app = Flask(__name__)


@app.route("/run")
def run():
    os.system(request.args.get("cmd", ""))  # comparison proof finding
    return "ok"
