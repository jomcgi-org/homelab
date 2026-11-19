import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, test, vi } from "vitest";

test("runtime diagnostics and fragmented output keep the token private", async () => {
  const token = "example-id-token";
  const log = vi.fn();
  const postMessage = vi.fn();
  const context = {
    console: { log },
    fetch: vi.fn(),
    process: {},
    fs: { writeSync: vi.fn() },
    importScripts: vi.fn(),
    TextDecoder,
    postMessage,
    WebAssembly: { instantiate: vi.fn() },
    Go: class {
      argv: string[] = [];
      exit!: () => void;
      run() {
        context.console.log(this.argv);
        for (const chunk of ["example-", "id-token\n"]) {
          context.fs.writeSync(2, new TextEncoder().encode(chunk));
        }
        this.exit();
      }
    },
  };
  Object.assign(context, { self: context });
  runInNewContext(readFileSync("public/kubectl-worker.js", "utf8"), context);
  await (context as typeof context & { onmessage: Function }).onmessage({
    data: { token, args: ["get", "pods"], server: "https://example.test" },
  });
  expect(log).not.toHaveBeenCalled();
  expect(postMessage).toHaveBeenCalledWith({ output: "[redacted]\n" });
  expect(JSON.stringify(postMessage.mock.calls)).not.toContain(token);
});
