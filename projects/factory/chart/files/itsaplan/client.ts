// Adapter for croffasia/itsaplan v1.10.0 packages/db/src/client.ts (AGPL-3.0-only).
// Mounted only in the API; migrations and the worker retain the upstream client.
import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import * as schema from "./schema";
import { transactionScope } from "./factory-transaction";

const connectionString = process.env.DATABASE_URL;
if (!connectionString) throw new Error("DATABASE_URL is not set");
const pool = drizzle(postgres(connectionString, { prepare: false }), {
  schema,
});
const scoped = transactionScope(pool);
export const db: typeof pool = scoped.db;
export const factoryTransaction = scoped.transaction;
export type DbExecutor =
  typeof db | Parameters<Parameters<typeof db.transaction>[0]>[0];
