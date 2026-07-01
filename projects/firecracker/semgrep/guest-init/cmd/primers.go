package main

import "github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"

// primerFiles is one small but realistic source file per supported extension. They
// are scanned once at base-build time (warmupPrime) BEFORE the snapshot is taken, so
// the one-time, per-process warmup the first real scan otherwise pays (lazy parser
// init per language, rule-matcher binding, OCaml runtime warmup) is captured in the
// snapshot memfile. A restored guest then scans its first real file already warmed.
//
// The content only needs to exercise each language's parser and the common rule
// paths (imports, a function/struct, a call, some control flow); it does not need to
// trigger findings. The __primer__ name prefix avoids colliding with a real scan
// path. Covers all extensions semgrep-guest-init recognises: .py .go .js .jsx .ts
// .tsx .rs (the language parsers behind them: python, go, javascript, typescript,
// rust).
var primerFiles = []vsockproto.ScanFile{
	{Path: "__primer__.py", Content: `import os
import subprocess


def run(cmd: str) -> int:
    proc = subprocess.run(["echo", cmd], capture_output=True)
    return proc.returncode


class Worker:
    def __init__(self, name: str) -> None:
        self.name = name

    def path(self) -> str:
        return os.path.join("/tmp", self.name)
`},
	{Path: "__primer__.go", Content: `package main

import (
	"fmt"
	"strings"
)

type greeter struct{ prefix string }

func (g greeter) greet(name string) string {
	return fmt.Sprintf("%s %s", g.prefix, strings.TrimSpace(name))
}

func main() {
	g := greeter{prefix: "hello"}
	fmt.Println(g.greet("world"))
}
`},
	{Path: "__primer__.js", Content: `const fs = require("fs");

function load(path) {
  const data = fs.readFileSync(path, "utf8");
  return JSON.parse(data);
}

module.exports = { load };
`},
	{Path: "__primer__.jsx", Content: `import React from "react";

export function Hello({ name }) {
  const label = name || "world";
  return <div className="hello">Hello {label}</div>;
}
`},
	{Path: "__primer__.ts", Content: `interface User {
  id: number;
  name: string;
}

export function format(u: User): string {
  return ` + "`${u.id}:${u.name}`" + `;
}
`},
	{Path: "__primer__.tsx", Content: `import React from "react";

type Props = { label: string; onClick: () => void };

export const Button = ({ label, onClick }: Props) => (
  <button onClick={onClick}>{label}</button>
);
`},
	{Path: "__primer__.rs", Content: `use std::collections::HashMap;

fn count(items: &[&str]) -> HashMap<String, u32> {
    let mut m = HashMap::new();
    for it in items {
        *m.entry(it.to_string()).or_insert(0) += 1;
    }
    m
}

fn main() {
    let _ = count(&["a", "b", "a"]);
}
`},
}
