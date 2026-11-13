/**
 * An in-memory backend for tests and the fixture preview. It replaces `fetch`
 * with a route table keyed like the real API (`METHOD /path/{param}`), typed
 * by the generated schema, so a fixture that drifts from the contract fails
 * typecheck. An unrouted call fails closed: the test or preview sees an error
 * instead of silently reaching a network.
 */
import type { Operation, Response as ApiResponse } from "../api/client";

export interface Call {
  operation: string;
  params: Record<string, string>;
  query: URLSearchParams;
  body: unknown;
}

type Reply<K extends Operation> =
  | ApiResponse<K>
  | { status: number; detail?: string }
  | ((
      call: Call,
    ) =>
      | ApiResponse<K>
      | { status: number; detail?: string }
      | Promise<ApiResponse<K> | { status: number; detail?: string }>);

export type Routes = { [K in Operation]?: Reply<K> };

/**
 * Routes outside the Grimoire API, keyed `METHOD /full/path/{param}`: the
 * join landing and the account API. Untyped, because the generated schema
 * covers only the Grimoire API.
 */
export type OtherRoutes = Record<string, unknown | ((call: Call) => unknown)>;

const isStatus = (
  value: unknown,
): value is { status: number; detail?: string } =>
  typeof value === "object" &&
  value !== null &&
  typeof (value as { status?: unknown }).status === "number" &&
  Object.keys(value).every((key) => key === "status" || key === "detail");

function match(template: string, path: string): Record<string, string> | null {
  const names: string[] = [];
  const pattern = template.replace(/\{(\w+)\}/g, (_match, name: string) => {
    names.push(name);
    return "([^/]+)";
  });
  const found = new RegExp(`^${pattern}$`).exec(path);
  if (!found) return null;
  return Object.fromEntries(
    names.map((name, index) => [name, decodeURIComponent(found[index + 1]!)]),
  );
}

export interface Backend {
  /** Every call made, oldest first. */
  calls: Call[];
  /** Calls to one operation. */
  callsTo: (operation: Operation | (string & {})) => Call[];
  /** Replace or add routes mid-test. */
  set: (routes: Routes) => void;
  restore: () => void;
}

export function installBackend(
  initial: Routes,
  other: OtherRoutes = {},
): Backend {
  const original = globalThis.fetch;
  let routes: Routes = { ...initial };
  const calls: Call[] = [];
  globalThis.fetch = async (input, init) => {
    const url = new URL(String(input), "https://friends.example.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const grimoire = url.pathname.startsWith("/api/grimoire/");
    const path = grimoire
      ? url.pathname.replace(/^\/api\/grimoire/, "")
      : url.pathname;
    for (const [operation, reply] of Object.entries(
      grimoire ? routes : other,
    )) {
      const [routeMethod, template] = operation.split(" ") as [string, string];
      if (routeMethod !== method) continue;
      const params = match(template, path);
      if (!params) continue;
      const call: Call = {
        operation,
        params,
        query: url.searchParams,
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : init?.body,
      };
      calls.push(call);
      const result =
        typeof reply === "function"
          ? await (reply as (call: Call) => unknown)(call)
          : reply;
      if (isStatus(result)) {
        return new Response(
          result.status === 204
            ? null
            : JSON.stringify({ detail: result.detail ?? "Refused." }),
          {
            status: result.status,
            headers: { "content-type": "application/json" },
          },
        );
      }
      if (result === undefined) return new Response(null, { status: 204 });
      return new Response(JSON.stringify(result), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    throw new Error(`Unrouted fixture call: ${method} ${url.pathname}`);
  };
  return {
    calls,
    callsTo: (operation) =>
      calls.filter((call) => call.operation === operation),
    set: (next) => {
      routes = { ...routes, ...next };
    },
    restore: () => {
      globalThis.fetch = original;
    },
  };
}
