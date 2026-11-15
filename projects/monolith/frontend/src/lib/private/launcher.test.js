import { describe, expect, it } from "vitest";
import { launcher } from "./launcher.js";

describe("private keypad", () => {
  it("reaches every private tool and the two factory entry points", () => {
    expect(
      launcher.filter((item) => !item.external).map((item) => item.href),
    ).toEqual([
      "/factory",
      "/factory/escalations",
      "/factory/execution",
      "/factory/execution?compose=1",
      "#ask",
      "/chat",
      "/review",
      "/notes",
      "/updates",
      "/qwen",
      "/grimoire",
    ]);
    expect(launcher.map((item) => item.label)).not.toContain("ArgoCD");
    expect(launcher.map((item) => item.label)).not.toContain("Longhorn");
  });

  it("marks the keys the dashboard fills in from live data", () => {
    expect(launcher.find((item) => item.label === "decisions").count).toBe(
      "waiting",
    );
    expect(launcher.find((item) => item.label === "conductor").dynamic).toBe(
      "conductor",
    );
  });

  it("links the organization repository and keeps external tools external", () => {
    expect(launcher.find((item) => item.label === "github")).toEqual({
      label: "github",
      href: "https://github.com/jomcgi-org/homelab",
      external: true,
    });
    for (const item of launcher.filter((item) => item.external)) {
      expect(new URL(item.href).protocol).toBe("https:");
    }
  });
});
