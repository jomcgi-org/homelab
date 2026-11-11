/**
 * The one way the UI talks to the backend.
 *
 * Operations are keyed `METHOD /path` and typed by the generated `schema.ts`,
 * so a wrong path, body or response shape fails typecheck. The session cookie
 * is the credential; the gateway validates it before the request arrives.
 */
import { signInExpired } from "../lib/session-expiry.js";
import { signIn } from "../app/signIn";
import type { Operations } from "./schema";

export type Operation = keyof Operations;
export type Response<K extends Operation> = Operations[K]["response"];
export type Body<K extends Operation> = Operations[K]["body"];
export type Query<K extends Operation> = Operations[K]["query"];

const API_ROOT = "/api/grimoire";
const TIMEOUT_MS = 15_000;

/** A request the backend refused. `message` is safe to show the player. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** The sign-in lapsed. Nothing was saved; `signIn` drives the recovery. */
export class SignInError extends ApiError {
  constructor() {
    super(401, "Your session has expired. Please sign in again.");
    this.name = "SignInError";
  }
}

export interface CallOptions<K extends Operation> {
  /** Values for the `{name}` segments of the path. */
  params?: Record<string, string>;
  query?: Query<K>;
  body?: Body<K>;
  signal?: AbortSignal;
}

/** Fills `{name}` segments. A missing or empty value is a programming error. */
export function resolvePath(
  template: string,
  params: Record<string, string> = {},
): string {
  return template.replace(/\{(\w+)\}/g, (_match, name: string) => {
    const value = params[name];
    if (!value) throw new Error(`Missing path parameter "${name}".`);
    return encodeURIComponent(value);
  });
}

function queryString(query: object | undefined): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null) search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

function detail(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const value = (body as { detail?: unknown }).detail;
  if (typeof value === "string") return value;
  // FastAPI validation errors: [{msg, loc}, ...].
  if (Array.isArray(value)) {
    const first = value[0] as { msg?: unknown } | undefined;
    if (typeof first?.msg === "string") return first.msg;
  }
  return null;
}

/** The URL of a GET operation, for `<img src>` and download links. */
export function apiUrl<K extends Operation>(
  operation: K,
  options: Pick<CallOptions<K>, "params" | "query"> = {},
): string {
  const path = operation.slice(operation.indexOf(" ") + 1);
  return (
    API_ROOT + resolvePath(path, options.params) + queryString(options.query)
  );
}

export async function api<K extends Operation>(
  operation: K,
  options: CallOptions<K> = {},
): Promise<Response<K>> {
  const method = operation.slice(0, operation.indexOf(" "));
  const upload = options.body instanceof FormData;
  const signals = [AbortSignal.timeout(TIMEOUT_MS)];
  if (options.signal) signals.push(options.signal);
  const response = await fetch(apiUrl(operation, options), {
    method,
    // A login redirect must surface as a sign-in signal, not be followed.
    redirect: "manual",
    credentials: "same-origin",
    signal: AbortSignal.any(signals),
    headers:
      options.body === undefined || upload
        ? undefined
        : { "content-type": "application/json" },
    body:
      options.body === undefined
        ? undefined
        : upload
          ? (options.body as FormData)
          : JSON.stringify(options.body),
  });
  if (signInExpired(response)) {
    signIn.signal();
    throw new SignInError();
  }
  signIn.answered();
  if (response.status === 204) return undefined as Response<K>;
  const type = response.headers.get("content-type") ?? "";
  // The gateway's static fallback must never answer under /api. If it does,
  // say so plainly instead of failing inside JSON.parse.
  if (!type.includes("json")) {
    throw new ApiError(
      response.status,
      "Grimoire answered with a page instead of data. Reload and try again.",
    );
  }
  const body: unknown = await response.json();
  if (!response.ok) {
    throw new ApiError(
      response.status,
      detail(body) ?? "Grimoire could not complete that request.",
    );
  }
  return body as Response<K>;
}

/** The message to show for any thrown value. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof TypeError || (error as Error)?.name === "TimeoutError")
    return "Connection interrupted. Nothing was lost; try again.";
  return "Something went wrong. Try again.";
}
