import { AsyncLocalStorage } from "node:async_hooks";

// Only the deferred route enters this scope. Upstream services keep using their
// shared db import, but all their awaited reads, writes and nested transactions
// use this request's transaction. Other requests and background jobs use the pool.
type Pool = {
  transaction<Result>(fn: (tx: object) => Promise<Result>): Promise<Result>;
};

export function transactionScope<Database extends Pool>(pool: Database) {
  const scope = new AsyncLocalStorage<object>();
  const db = new Proxy(pool, {
    get(target, key) {
      const executor = scope.getStore() ?? target;
      const value = Reflect.get(executor, key, executor);
      return typeof value === "function" ? value.bind(executor) : value;
    },
  });
  return {
    db,
    transaction: <Result>(fn: () => Promise<Result>): Promise<Result> =>
      pool.transaction((tx) => scope.run(tx, fn)),
  };
}
