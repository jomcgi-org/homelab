import { describe, it, expect } from "vitest";
import { createConstellationStore } from "./constellation-store.js";

const strahd = {
  id: 7,
  title: "Strahd von Zarovich",
  kind: "entity",
  entity_type: "npc",
};
const ireena = {
  id: 8,
  title: "Ireena Kolyana",
  kind: "entity",
  entity_type: "npc",
};
const chunk = { id: 1, title: "Chapter 1", kind: "chunk" };

// Plain-object shim: enough of the Storage contract (getItem/setItem/
// removeItem) for the store, with no jsdom/browser dependency (vitest.config
// runs the "node" environment, which has no sessionStorage global by
// default -- exactly the SSR condition the store must degrade gracefully
// under).
function makeStorageShim() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
    clear: () => data.clear(),
  };
}

function withGlobalSessionStorage(shim, fn) {
  const had = "sessionStorage" in globalThis;
  const prev = had ? globalThis.sessionStorage : undefined;
  globalThis.sessionStorage = shim;
  try {
    return fn();
  } finally {
    if (had) globalThis.sessionStorage = prev;
    else delete globalThis.sessionStorage;
  }
}

describe("constellation-store", () => {
  it("has no sessionStorage in the bare test environment (SSR-like)", () => {
    expect(typeof sessionStorage).toBe("undefined");
  });

  describe("SSR guard (no sessionStorage global)", () => {
    it("works fully in-memory without throwing", () => {
      expect(() => {
        const store = createConstellationStore();
        const seen = [];
        const unsub = store.subscribe((s) => seen.push(s));
        store.touch(strahd);
        unsub();
        expect(seen.at(-1).nodes.length).toBe(1);
      }).not.toThrow();
    });
  });

  describe("touch", () => {
    it("adds a node once (dedupes by id)", () => {
      withGlobalSessionStorage(makeStorageShim(), () => {
        const store = createConstellationStore();
        let last;
        store.subscribe((s) => (last = s));
        store.touch(strahd);
        store.touch({ ...strahd, title: "renamed" });
        expect(last.nodes.length).toBe(1);
        expect(last.nodes[0].name).toBe("Strahd von Zarovich");
      });
    });

    it("ignores non-entity kinds", () => {
      withGlobalSessionStorage(makeStorageShim(), () => {
        const store = createConstellationStore();
        let last;
        store.subscribe((s) => (last = s));
        store.touch(chunk);
        expect(last.nodes).toEqual([]);
      });
    });

    it("ignores malformed items without throwing", () => {
      withGlobalSessionStorage(makeStorageShim(), () => {
        const store = createConstellationStore();
        expect(() => store.touch(null)).not.toThrow();
        expect(() => store.touch({ kind: "entity" })).not.toThrow();
      });
    });
  });

  describe("recordEgo", () => {
    it("recomputes edges from stored egos", () => {
      withGlobalSessionStorage(makeStorageShim(), () => {
        const store = createConstellationStore();
        let last;
        store.subscribe((s) => (last = s));
        store.touch(strahd);
        store.touch(ireena);
        store.recordEgo(7, { edges: [{ from: 7, to: 8 }] });
        expect(last.edges).toEqual([{ from: 7, to: 8 }]);
      });
    });

    it("drops edges to entities not yet in the session", () => {
      withGlobalSessionStorage(makeStorageShim(), () => {
        const store = createConstellationStore();
        let last;
        store.subscribe((s) => (last = s));
        store.touch(strahd);
        store.recordEgo(7, { edges: [{ from: 7, to: 999 }] });
        expect(last.edges).toEqual([]);
      });
    });
  });

  describe("clear", () => {
    it("resets to empty and persists the empty state", () => {
      const shim = makeStorageShim();
      withGlobalSessionStorage(shim, () => {
        const store = createConstellationStore();
        store.touch(strahd);
        store.clear();
        const stored = JSON.parse(shim.getItem("grimoire.constellation"));
        expect(stored.nodes).toEqual([]);
      });

      withGlobalSessionStorage(shim, () => {
        const fresh = createConstellationStore();
        let last;
        fresh.subscribe((s) => (last = s));
        expect(last.nodes).toEqual([]);
      });
    });
  });

  describe("serialization round-trip", () => {
    it("persists nodes, edges, and egos, then hydrates a fresh store from the same storage", () => {
      const shim = makeStorageShim();
      withGlobalSessionStorage(shim, () => {
        const store = createConstellationStore();
        store.touch(strahd);
        store.touch(ireena);
        store.recordEgo(7, { edges: [{ from: 7, to: 8 }] });
      });

      withGlobalSessionStorage(shim, () => {
        const fresh = createConstellationStore();
        let last;
        fresh.subscribe((s) => (last = s));
        expect(last.nodes.length).toBe(2);
        expect(last.ids.has(7)).toBe(true);
        expect(last.ids.has(8)).toBe(true);
        expect(last.edges).toEqual([{ from: 7, to: 8 }]);

        // A subsequent ego fetch on the rehydrated store still recomputes
        // correctly (egos Map round-tripped, not just the derived edges).
        fresh.recordEgo(8, { edges: [{ from: 8, to: 7 }] });
        expect(last.edges.length).toBe(1);
      });
    });
  });

  describe("corrupt storage", () => {
    it("falls back to empty state instead of throwing", () => {
      const shim = makeStorageShim();
      shim.setItem("grimoire.constellation", "{not valid json");
      withGlobalSessionStorage(shim, () => {
        expect(() => {
          const store = createConstellationStore();
          let last;
          store.subscribe((s) => (last = s));
          expect(last.nodes).toEqual([]);
          expect(last.edges).toEqual([]);
        }).not.toThrow();
      });
    });

    it("tolerates a well-formed-JSON-but-wrong-shape payload", () => {
      const shim = makeStorageShim();
      shim.setItem("grimoire.constellation", JSON.stringify({ foo: "bar" }));
      withGlobalSessionStorage(shim, () => {
        const store = createConstellationStore();
        let last;
        store.subscribe((s) => (last = s));
        expect(last.nodes).toEqual([]);
        expect(last.edges).toEqual([]);
      });
    });

    it("tolerates a sessionStorage that throws on access", () => {
      const throwing = {
        getItem: () => {
          throw new Error("blocked");
        },
        setItem: () => {
          throw new Error("blocked");
        },
      };
      withGlobalSessionStorage(throwing, () => {
        expect(() => {
          const store = createConstellationStore();
          store.touch(strahd);
        }).not.toThrow();
      });
    });
  });
});
