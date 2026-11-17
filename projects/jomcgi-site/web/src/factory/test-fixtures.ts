import { vi } from "vitest";
import manifestRaw from "../../../../factory-storyboard/fixtures/manifest.json?raw";
import type {
  Manifest,
  ObjectPath,
} from "../../../../factory-storyboard/schema/types";
import { validate } from "../../../../factory-storyboard/schema/validate";
import { createLoader, sha256 } from "./data";

const objects = import.meta.glob<string>(
  "../../../../factory-storyboard/fixtures/objects/*.json",
  {
    query: "?raw",
    import: "default",
    eager: true,
  },
);
export function fixtureFiles(): Map<string, string> {
  return new Map([
    ["/data/factory/manifest.json", manifestRaw],
    ...Object.entries(objects).map(
      ([path, raw]) =>
        [`/data/factory/objects/${path.split("/").at(-1)!}`, raw] as [
          string,
          string,
        ],
    ),
  ]);
}
export function fixtureManifest(): Manifest {
  const data: unknown = JSON.parse(manifestRaw);
  if (!validate("manifest", data))
    throw new Error("Fixture manifest is invalid");
  return data;
}
export function mockFiles(files = fixtureFiles()) {
  const mock = vi.fn(
    async (url: string) =>
      new Response(files.get(url) ?? "Not found", {
        status: files.has(url) ? 200 : 404,
      }),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}
export async function fixtureSnapshot() {
  mockFiles();
  return createLoader()(new AbortController().signal);
}
export async function replaceObject(
  files: Map<string, string>,
  oldPath: ObjectPath,
  value: unknown,
): Promise<ObjectPath> {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  const path: ObjectPath = `objects/${await sha256(bytes.buffer)}.json`;
  files.set(`/data/factory/${path}`, new TextDecoder().decode(bytes));
  files.set(
    "/data/factory/manifest.json",
    files.get("/data/factory/manifest.json")!.replaceAll(oldPath, path),
  );
  return path;
}
