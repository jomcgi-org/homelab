import { open, readFile, rename, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { join } from "node:path";

// The retained ReadWriteOncePod PVC serializes the apply and recovery pods.
// Atomic replacement plus file and directory fsync survive a killed Job.
export function snapshotStore(directory) {
  const path = join(directory, "snapshot.json");
  return {
    async read() {
      try {
        const saved = JSON.parse(await readFile(path, "utf8"));
        if (!saved || typeof saved !== "object")
          throw new Error("Invalid edge snapshot");
        return saved;
      } catch (error) {
        if (error.code === "ENOENT") return null;
        throw error;
      }
    },
    async write(snapshot) {
      const temporary = join(directory, `.snapshot-${randomUUID()}.json`);
      const handle = await open(temporary, "wx", 0o600);
      try {
        await handle.writeFile(JSON.stringify(snapshot));
        await handle.sync();
        await handle.close();
        await rename(temporary, path);
        const parent = await open(directory, "r");
        try {
          await parent.sync();
        } finally {
          await parent.close();
        }
      } finally {
        await handle.close();
        await unlink(temporary).catch((error) => {
          if (error.code !== "ENOENT") throw error;
        });
      }
    },
  };
}
