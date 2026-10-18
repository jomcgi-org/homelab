import { describe, expect, it } from "vitest";
import { launcher } from "./launcher.js";

describe("private launcher", () => {
  it("offers only current internal destinations", () => {
    expect(
      launcher.filter((item) => !item.external).map((item) => item.href),
    ).toEqual(["/factory", "/qwen", "/grimoire", "/updates"]);
    expect(launcher.map((item) => item.label)).not.toContain("ArgoCD");
    expect(launcher.map((item) => item.label)).not.toContain("Longhorn");
  });

  it("links the organization repository and keeps external tools external", () => {
    expect(launcher.find((item) => item.label === "GitHub")).toEqual({
      label: "GitHub",
      desc: "jomcgi-org/homelab",
      href: "https://github.com/jomcgi-org/homelab",
      external: true,
    });
    for (const item of launcher.filter((item) => item.external)) {
      expect(new URL(item.href).protocol).toBe("https:");
    }
  });
});
