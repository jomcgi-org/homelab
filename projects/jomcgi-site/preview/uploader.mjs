// The PR preview uploader (README "PR previews"). A session posts the built
// site for its PR; this process checks the PR, checks the files, uploads
// them as preview alias pr-<N> of the dev Worker and returns the URL. The
// session writes the PR comment with its own GitHub access.
//
// Order of checks. Each step must pass before the next one runs:
//   1. pr and sha are well formed.
//   2. GitHub (public API, no credential): the PR is open, from this
//      repository (not a fork), by a trusted author, and sha is its head.
//   3. The archive holds static files only (archive.mjs).
//   4. Cloudflare Access denies an anonymous request to the alias host.
//   5. wrangler uploads the version with main's wrangler.jsonc (the one in
//      this image), never a config from the archive.
//   6. Access still denies an anonymous request, and the service token gets
//      the page.
//   7. Step 2 passes again; then the URL and sha go back to the caller.
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { ArchiveError, readPreview } from "./archive.mjs";
import { GitHubError } from "./github.mjs";

export const TRUSTED_AUTHORS = new Set(["jomcgi", "jomcgi-factory[bot]"]);
export const WORKER = "jomcgi-site-dev";
const CF_API = "https://api.cloudflare.com/client/v4";

export class Refused extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// An Access-protected host answers an anonymous request with a redirect to
// the team's cloudflareaccess.com login, or 401/403. Anything else, including
// a network error, counts as open.
export async function anonymousDenied(url, fetchImpl = fetch) {
  let response;
  try {
    response = await fetchImpl(url, {
      redirect: "manual",
      signal: AbortSignal.timeout(20_000),
    });
  } catch {
    return false;
  }
  if (response.status === 401 || response.status === 403) return true;
  if (![301, 302, 303, 307, 308].includes(response.status)) return false;
  try {
    const location = new URL(response.headers.get("location") ?? "", url);
    return (
      location.protocol === "https:" &&
      location.hostname.endsWith(".cloudflareaccess.com")
    );
  } catch {
    return false;
  }
}

async function serviceTokenReaches(url, access, fetchImpl) {
  try {
    const response = await fetchImpl(url, {
      redirect: "manual",
      headers: {
        "CF-Access-Client-Id": access.clientId,
        "CF-Access-Client-Secret": access.clientSecret,
      },
      signal: AbortSignal.timeout(20_000),
    });
    return response.status === 200;
  } catch {
    return false;
  }
}

// The account's workers.dev subdomain names the alias host before upload.
export async function workersSubdomain({ token, account }, fetchImpl = fetch) {
  const response = await fetchImpl(
    `${CF_API}/accounts/${account}/workers/subdomain`,
    {
      headers: { Authorization: `Bearer ${token}` },
      redirect: "error",
      signal: AbortSignal.timeout(30_000),
    },
  );
  const body = await response.json().catch(() => ({}));
  const subdomain = body?.result?.subdomain;
  if (
    !response.ok ||
    typeof subdomain !== "string" ||
    !/^[a-z0-9-]+$/.test(subdomain)
  ) {
    throw new Error(
      `Cloudflare workers subdomain lookup failed: HTTP ${response.status}`,
    );
  }
  return subdomain;
}

// Runs `wrangler versions upload` and returns the alias URL wrangler reports.
export function wranglerUpload({ wrangler, configSource, workRoot, env }) {
  return async ({ files, pr, sha }) => {
    const work = await mkdtemp(join(workRoot, "preview-"));
    try {
      const dist = join(work, "web", "dist");
      for (const [path, data] of files) {
        await mkdir(dirname(join(dist, path)), { recursive: true });
        await writeFile(join(dist, path), data);
      }
      await writeFile(
        join(work, "wrangler.jsonc"),
        await readFile(configSource),
      );
      const output = join(work, "wrangler-output.json");
      const args = [
        "versions",
        "upload",
        "--config",
        join(work, "wrangler.jsonc"),
        "--env",
        "dev",
        "--preview-alias",
        `pr-${pr}`,
        "--tag",
        `pr-${pr}`,
        "--message",
        `PR #${pr} preview ${sha}`,
      ];
      const code = await new Promise((resolve, reject) => {
        const child = spawn(wrangler, args, {
          cwd: work,
          env: { ...env, WRANGLER_OUTPUT_FILE_PATH: output, HOME: work },
          stdio: ["ignore", "inherit", "inherit"],
          timeout: 10 * 60_000,
        });
        child.on("error", reject);
        child.on("exit", resolve);
      });
      if (code !== 0)
        throw new Error(`wrangler versions upload exited ${code}`);
      const records = (await readFile(output, "utf8"))
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line));
      const upload = records.find((r) => r.type === "version-upload");
      return upload?.preview_alias_url;
    } finally {
      await rm(work, { recursive: true, force: true });
    }
  };
}

// Step 2. GitHub errors become refusals, so a rate limit fails closed.
async function checkPull(github, pr, sha, when) {
  let pull;
  try {
    pull = await github.pull(pr);
  } catch (error) {
    if (error instanceof GitHubError)
      throw new Refused(error.status, error.message);
    throw error;
  }
  if (pull.state !== "open") throw new Refused(409, `PR #${pr} is not open`);
  if (pull.head?.repo?.full_name !== pull.base?.repo?.full_name) {
    throw new Refused(403, `PR #${pr} comes from a fork`);
  }
  if (!TRUSTED_AUTHORS.has(pull.user?.login)) {
    throw new Refused(
      403,
      `PR #${pr} author ${pull.user?.login} is not trusted for previews`,
    );
  }
  if (pull.head?.sha !== sha) {
    throw new Refused(
      409,
      when === "after"
        ? `PR #${pr} moved to ${pull.head?.sha} during the upload; rebuild and retry`
        : `${sha} is not the head of PR #${pr} (${pull.head?.sha}); push, then retry`,
    );
  }
}

// deps: { github, subdomain(), upload({files, pr, sha}), fetchImpl, access }
export async function handlePreview({ pr, sha, body }, deps) {
  if (!/^[1-9][0-9]{0,6}$/.test(pr ?? ""))
    throw new Refused(400, "pr must be a PR number");
  if (!/^[0-9a-f]{40}$/.test(sha ?? ""))
    throw new Refused(400, "sha must be a full commit SHA");
  if (!deps.access) {
    throw new Refused(
      503,
      "the uploader has no Access service token; nothing was uploaded",
    );
  }

  await checkPull(deps.github, pr, sha, "before");

  let files;
  try {
    files = readPreview(body);
  } catch (error) {
    if (error instanceof ArchiveError) throw new Refused(422, error.message);
    throw error;
  }

  const expected = `https://pr-${pr}-${WORKER}.${await deps.subdomain()}.workers.dev`;
  if (!(await anonymousDenied(`${expected}/`, deps.fetchImpl))) {
    throw new Refused(
      503,
      `Cloudflare Access does not protect ${expected}; nothing was uploaded`,
    );
  }

  const url = await deps.upload({ files, pr, sha });
  if (url !== expected) {
    throw new Refused(
      502,
      `wrangler reported alias URL ${url}, expected ${expected}; previews may be off for ${WORKER}`,
    );
  }
  if (!(await anonymousDenied(`${url}/`, deps.fetchImpl))) {
    throw new Refused(
      503,
      `the preview answered an anonymous request; the URL is withheld`,
    );
  }
  if (!(await serviceTokenReaches(`${url}/`, deps.access, deps.fetchImpl))) {
    throw new Refused(
      502,
      `the factory service token could not open the preview; the URL is withheld`,
    );
  }

  await checkPull(deps.github, pr, sha, "after");
  return { url: `${url}/`, sha };
}
