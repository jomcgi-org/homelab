// @vitest-environment happy-dom
import { createApp } from "vue";
import { test, expect, vi } from "vitest";
import TerminalPage from "./src/TerminalPage.vue";
const token = "example-id-token";
let input: (data: string) => void;
const write = vi.fn();
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    write = write;
    writeln() {}
    focus() {}
    open() {}
    dispose() {}
    onData(handler: typeof input) {
      input = handler;
    }
  },
}));
vi.mock("./src/apiConfig", () => ({ getIdToken: async () => token }));
vi.mock("./src/kubectl", () => ({ loadKubectl: async () => ({}) }));
test("history recalls the typed command without credentials", async () => {
  const send = vi.fn();
  let done: () => void;
  vi.stubGlobal(
    "Worker",
    class {
      onmessage!: Function;
      postMessage(data: unknown) {
        send(data);
        done = () => this.onmessage({ data: { done: true } });
      }
      terminate() {}
    },
  );
  const app = createApp(TerminalPage, { initial: "kubectl get pods" });
  try {
    app.mount(document.createElement("div"));
    input("\r");
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    done!();
    input("kubectl get pods " + token);
    input("\r");
    await vi.waitFor(() => expect(write.mock.lastCall).toEqual(["$ "]));
    input("\x1b[A");
    expect(write.mock.lastCall).toEqual(["\r\x1b[2K$ kubectl get pods"]);
    expect(send).toHaveBeenCalledTimes(1);
  } finally {
    app.unmount();
    vi.unstubAllGlobals();
  }
});
