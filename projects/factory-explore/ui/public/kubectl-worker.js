// Each command owns its Go runtime. The page retains the compiled module.
self.onmessage = async ({ data }) => {
  try {
    if (data.compile) {
      const response = await fetch("/kubectl.wasm.gz");
      if (!response.ok) throw new Error();
      const stream = response.body.pipeThrough(new DecompressionStream("gzip"));
      const module = await WebAssembly.compileStreaming(
        new Response(stream, {
          headers: { "Content-Type": "application/wasm" },
        }),
      );
      postMessage({ module });
      return;
    }
    importScripts("/wasm_exec.js");
    const { module, args, token, server } = data;
    const request = self.fetch.bind(self);
    self.fetch = async (url, options) => {
      if (new URL(url).origin !== server)
        throw new Error("Unexpected API origin");
      const response = await request(url, { ...options, redirect: "error" });
      postMessage({
        status: response.status,
        end: response.headers.get("X-Explore-Window-End"),
        next: response.headers.get("X-Explore-Next-Window"),
      });
      return response;
    };
    // client-go probes kubeconfig and discovery-cache paths before fetching.
    // wasm_exec's default ENOSYS describes a broken filesystem, whereas this
    // worker has an empty filesystem.
    self.process.cwd = () => "/";
    for (const method of ["stat", "lstat", "open", "readdir"]) {
      self.fs[method] = (...args) => {
        const callback = args.pop();
        callback(
          Object.assign(new Error("File does not exist"), { code: "ENOENT" }),
        );
      };
    }
    const decoder = new TextDecoder();
    let pending = "";
    function output(value) {
      postMessage({ output: value.replaceAll(token, "[redacted]") });
    }
    self.fs.writeSync = (_, bytes) => {
      pending += decoder.decode(bytes, { stream: true });
      const end = pending.lastIndexOf("\n") + 1;
      if (end) output(pending.slice(0, end));
      pending = pending.slice(end);
      return bytes.length;
    };
    const go = new Go();
    go.env = { HOME: "/" };
    go.argv = ["kubectl", "--server=" + server, "--token=" + token, ...args];
    go.exit = () => {
      output(pending + decoder.decode());
      postMessage({ done: true });
    };
    await go.run(await WebAssembly.instantiate(module, go.importObject));
  } catch {
    postMessage({ failed: true });
  }
};
