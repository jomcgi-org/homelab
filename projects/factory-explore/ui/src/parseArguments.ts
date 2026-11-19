// Split a command line into argv without evaluating shell expressions.
export function parseArguments(line: string): string[] {
  const args: string[] = [];
  let word = "";
  let quote = "";
  let started = false;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (!quote && /\s/.test(char)) {
      if (started) args.push(word);
      started = false;
      word = "";
      continue;
    }
    started = true;
    if (char === quote) {
      quote = "";
    } else if (!quote && (char === "'" || char === '"')) {
      quote = char;
    } else if (char === "\\" && quote !== "'") {
      const next = line[i + 1];
      if (next === undefined) throw new Error("Incomplete escape");
      if (quote && !['"', "\\", "$", "`"].includes(next)) word += char;
      else {
        word += next;
        i++;
      }
    } else {
      word += char;
    }
  }
  if (quote) throw new Error("Unclosed quote");
  if (started) args.push(word);
  return args;
}
