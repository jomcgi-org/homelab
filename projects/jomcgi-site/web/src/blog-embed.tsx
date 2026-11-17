import { useEffect, useRef, useState } from "react";
export type EmbedKind = "conformance" | "research" | "coding";
type Dispose = () => void;
export type EmbedLoader = (
  target: HTMLElement,
  kind: EmbedKind,
) => Promise<Dispose>;
const loadEmbed: EmbedLoader = async (target, kind) => {
  const { mount, unmount } = await import("svelte");
  if (kind === "conformance") {
    const { default: Component } =
      await import("./embeds/ConformanceReplay.svelte");
    const instance = mount(Component, { target });
    return () => {
      void unmount(instance);
    };
  }
  const [{ default: Component }, { default: recording }] = await Promise.all([
    import("./embeds/QwenReplay.svelte"),
    kind === "coding"
      ? import("./embeds/agent-replay.json")
      : import("./embeds/qwen-replay.json"),
  ]);
  const instance = mount(Component, {
    target,
    props: { kind, recording, landing: true },
  });
  return () => {
    void unmount(instance);
  };
};
export function BlogEmbed({
  kind,
  label,
  loader = loadEmbed,
}: {
  kind: EmbedKind;
  label: string;
  loader?: EmbedLoader;
}) {
  const target = useRef<HTMLDivElement>(null);
  const [requested, setRequested] = useState(false);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "error">(
    "idle",
  );
  useEffect(() => {
    if (!requested) return;
    let disposed = false;
    let cleanup: Dispose | undefined;
    // A private host isolates an in-flight import from a later retry/unmount.
    const host = document.createElement("div");
    target.current!.append(host);
    setState("loading");
    loader(host, kind)
      .then((dispose) => {
        if (disposed) dispose();
        else {
          cleanup = dispose;
          setState("ready");
        }
      })
      .catch(() => {
        if (!disposed) {
          host.replaceChildren();
          setState("error");
          setRequested(false);
        }
      });
    return () => {
      disposed = true;
      cleanup?.();
      host.remove();
    };
  }, [requested, loader, kind]);
  return (
    <section className="blog-embed" aria-label={label}>
      {state !== "ready" && (
        <button
          type="button"
          disabled={requested}
          onClick={() => setRequested(true)}
        >
          {state === "loading"
            ? "Loading…"
            : `${state === "error" ? "Retry" : "Load"} ${label}`}
        </button>
      )}
      {state === "error" && (
        <p role="alert">The recording could not load. Try again.</p>
      )}
      <div ref={target} />
    </section>
  );
}
