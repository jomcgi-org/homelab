# Factory browser interface comparison

On Linux with procfs, Node 22, npm, Python 3 and Chromium installed, run
these commands from this directory in a disposable checkout.

```sh
npm ci --ignore-scripts
CHROMIUM=/absolute/path/to/chromium python3 compare.py
```

The runner serves the checked-in HTML on an ephemeral loopback port. Each
trial opens a fresh CLI session or isolated stdio MCP session, navigates,
reads the heading from an accessibility snapshot and captures a 640×480
viewport. It decodes the PNG with CRC checking and verifies the fixture
background at the bottom right. JSON output records screenshot and fixture
digests and the browser executable's exact version.

Normal trials close the interface. Interrupt trials deliver SIGINT or expire
a one-second timer with the session open after screenshot validation. The
Linux supervisor adopts detached descendants, kills and reaps remaining
children, and checks its child list before returning. Each trial has a
45-second task deadline and a separate ten-second cleanup deadline.
