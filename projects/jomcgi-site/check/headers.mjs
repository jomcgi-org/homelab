import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

// The Workers asset server applies _headers rules in file order: a matching
// rule first detaches its `! Name` headers, then sets its own. Site-check
// derives each path's expected headers from the same file, so a section with
// its own policy (for example the /hikes map) needs no change here.
export function parseHeaders(text) {
  const rules = [];
  for (const line of text.split("\n")) {
    if (!line.trim() || line.trimStart().startsWith("#")) continue;
    if (!line.startsWith(" ")) {
      const expression = line
        .split(/(\*|:[a-zA-Z]\w*)/)
        .map((token) =>
          token === "*"
            ? ".*"
            : token.startsWith(":")
              ? "[^/]+"
              : token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
        )
        .join("");
      rules.push({
        host: line.startsWith("https://"),
        match: new RegExp(`^${expression}$`),
        detach: [],
        headers: {},
      });
    } else {
      const detach = line.match(/^\s+!\s*([\w-]+)\s*$/);
      if (detach) {
        rules.at(-1).detach.push(detach[1].toLowerCase());
        continue;
      }
      const [, name, value] = line.match(/^\s+([\w-]+): (.+)$/);
      rules.at(-1).headers[name.toLowerCase()] = value;
    }
  }
  return rules;
}

export function headersFor(rules, path, preview = true) {
  const origin = preview ? "https://next.jomcgi.dev" : "https://jomcgi.dev";
  const url = new URL(path, origin);
  const applied = {};
  for (const rule of rules) {
    const target = rule.host ? `${url.origin}${url.pathname}` : url.pathname;
    if (!rule.match.test(target)) continue;
    for (const name of rule.detach) delete applied[name];
    Object.assign(applied, rule.headers);
  }
  return applied;
}

/** The release's _headers: the built copy when present, else the source. */
export function releaseHeaderRules(dist) {
  const built = dist && join(dist, "_headers");
  const file =
    built && existsSync(built)
      ? built
      : new URL("../web/public/_headers", import.meta.url);
  return parseHeaders(readFileSync(file, "utf8"));
}
