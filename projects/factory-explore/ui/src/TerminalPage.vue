<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from "vue";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { parseArguments } from "./parseArguments";
import { loadKubectl } from "./kubectl";
import { getIdToken, observeResponse } from "./apiConfig";
const props = defineProps<{ initial: string }>();
const host = ref<HTMLElement>();
const term = new Terminal({ convertEol: true, cursorBlink: true });
let line = props.initial;
const history: string[] = [];
let cursor = 0;
let generation = 0;
let busy = false;
let worker: Worker | undefined;
function finish() {
  generation++;
  worker?.terminate();
  worker = undefined;
  busy = false;
  term.write("$ ");
}
async function execute() {
  const command = line;
  line = "";
  term.writeln("");
  if (!command.trim()) return finish();
  let args: string[];
  try {
    args = parseArguments(command);
  } catch {
    term.writeln("Complete the quote or escape before running kubectl.");
    return finish();
  }
  if (
    args.shift() !== "kubectl" ||
    args.some(
      (arg) =>
        /^--(?:token|server|kubeconfig|password)(?:=|$)/.test(arg) ||
        /^-s/.test(arg),
    )
  ) {
    term.writeln("Enter a kubectl command using the explorer connection.");
    return finish();
  }
  busy = true;
  const id = ++generation;
  try {
    const [module, token] = await Promise.all([loadKubectl(), getIdToken()]);
    if (id !== generation) return;
    if (!command.includes(token)) history.push(command);
    cursor = history.length;
    worker = new Worker("/kubectl-worker.js");
    worker.onerror = () => {
      term.writeln("kubectl failed");
      finish();
    };
    worker.onmessage = ({ data }) => {
      if (id !== generation) return;
      if (data.status) void observeResponse(data.status, data.end, data.next);
      if (data.output) term.write(data.output);
      if (data.failed) term.writeln("kubectl failed");
      if (data.failed || data.done) finish();
    };
    worker.postMessage({
      module,
      token,
      args,
      server: import.meta.env.VITE_KUBERNETES_API,
    });
  } catch {
    if (id === generation) {
      term.writeln("Unable to start kubectl");
      finish();
    }
  }
}
onMounted(() => {
  term.open(host.value!);
  term.writeln(
    "kubectl get, describe, explain, events, api-resources, api-versions, top, auth, version",
  );
  term.write("$ " + line);
  term.focus();
  void loadKubectl().catch(() => {});
  term.onData((data) => {
    if (data === "\x03") {
      line = "";
      term.writeln("^C");
      finish();
      return;
    }
    if (busy) return;
    if (data === "\r") {
      void execute();
      return;
    }
    if (data === "\x1b[A" || data === "\x1b[B") {
      cursor = Math.max(
        0,
        Math.min(history.length, cursor + (data.endsWith("A") ? -1 : 1)),
      );
      line = history[cursor] ?? "";
      term.write("\r\x1b[2K$ " + line);
    } else if (data === "\x7f" && line) {
      line = line.slice(0, -1);
      term.write("\b \b");
    } else if (!/[\x00-\x1f\x7f]/.test(data)) {
      line += data;
      term.write(data);
    }
  });
});
onBeforeUnmount(() => {
  generation++;
  worker?.terminate();
  term.dispose();
});
</script>
<template><div ref="host" aria-label="kubectl terminal"></div></template>
