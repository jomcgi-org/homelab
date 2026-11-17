import { expect, it } from "vitest";
import { createLoader, fillStats } from "./data";
import {
  fixtureFiles,
  fixtureManifest,
  mockFiles,
  replaceObject,
} from "./test-fixtures";

it("fills repeated, zero and fractional stats and uses a dash for unknown keys", () => {
  expect(
    fillStats(
      "{{stat:zero}} / {{stat:hours}} / {{stat:zero}} / {{stat:missing}} / {{stat:constructor}}",
      {
        zero: { label: "Zero", value: 0, unit: "issues" },
        hours: { label: "Hours", value: 12.5, unit: "hours" },
      },
    ),
  ).toBe("0 / 12.5 / 0 / – / –");
});

it("loads the manifest first, verifies each object, and reuses immutable objects", async () => {
  const fetch = mockFiles();
  const load = createLoader();
  const signal = new AbortController().signal;
  const snapshot = await load(signal);
  expect(fetch.mock.calls[0]![0]).toBe("/data/factory/manifest.json");
  expect(fetch).toHaveBeenCalledTimes(11);
  expect(snapshot.story?.kind).toBe("story");
  expect(snapshot.goals["reading-list"]?.kind).toBe("goal");
  expect(snapshot.issues["DEMO-104"]?.kind).toBe("issue");
  expect(snapshot.sessions["demo-build"]?.kind).toBe("session");
  await load(signal);
  expect(fetch).toHaveBeenCalledTimes(12);
});

it("isolates missing, malformed, wrongly addressed and mismatched objects", async () => {
  const files = fixtureFiles();
  const manifest = fixtureManifest();
  files.delete(`/data/factory/${manifest.goals["reading-list"]!}`);
  await replaceObject(files, manifest.issues["DEMO-101"]!, {
    version: 1,
    kind: "issue",
    key: "DEMO-101",
  });
  files.set(`/data/factory/${manifest.sessions["demo-build"]!}`, "broken JSON");
  const other = JSON.parse(
    files.get(`/data/factory/${manifest.issues["DEMO-102"]!}`)!,
  );
  await replaceObject(files, manifest.issues["DEMO-102"]!, {
    ...other,
    key: "DEMO-999",
  });
  mockFiles(files);
  const snapshot = await createLoader()(new AbortController().signal);
  expect(snapshot.goals["reading-list"]).toBeNull();
  expect(snapshot.issues["DEMO-101"]).toBeNull();
  expect(snapshot.issues["DEMO-102"]).toBeNull();
  expect(snapshot.sessions["demo-build"]).toBeNull();
  expect(snapshot.story?.headline).toBe("A reading list that keeps your place");
  expect(snapshot.issues["DEMO-104"]?.state).toBe("Shipped");
});

it("retries failed objects even when the manifest stays the same", async () => {
  const files = fixtureFiles();
  const path = `/data/factory/${fixtureManifest().sessions["demo-build"]!}`;
  const saved = files.get(path)!;
  files.delete(path);
  mockFiles(files);
  const load = createLoader();
  expect(
    (await load(new AbortController().signal)).sessions["demo-build"],
  ).toBeNull();
  files.set(path, saved);
  expect(
    (await load(new AbortController().signal)).sessions["demo-build"]?.turns
      .length,
  ).toBe(28);
});

it("rejects invalid manifests without following their object paths", async () => {
  const files = fixtureFiles();
  files.set(
    "/data/factory/manifest.json",
    JSON.stringify({
      ...fixtureManifest(),
      story: "https://example.invalid/secret.json",
    }),
  );
  const fetch = mockFiles(files);
  await expect(createLoader()(new AbortController().signal)).rejects.toThrow(
    "Manifest not available",
  );
  expect(fetch).toHaveBeenCalledTimes(1);
});
