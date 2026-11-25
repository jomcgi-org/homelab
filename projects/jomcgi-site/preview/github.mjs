// Reads one pull request of the public repository through GitHub's
// unauthenticated API. The uploader holds no GitHub credential.
//
// The anonymous limit is 60 requests an hour per address. The uploader keeps
// each PR's last answer and ETag for a short time, and the next read of that
// PR sends If-None-Match: a 304 answer does not count against the limit, and
// the kept body is still current. A 403 or 429 (rate limit or refusal), or
// no answer, fails closed.

const API = "https://api.github.com";
export const CACHE_MS = 10 * 60_000;

export class GitHubError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// repository: "owner/name".
export function publicGitHub({
  repository,
  fetchImpl = fetch,
  now = () => Date.now(),
}) {
  const cache = new Map();

  return {
    async pull(number) {
      for (const [key, entry] of cache) {
        if (now() - entry.at > CACHE_MS) cache.delete(key);
      }
      const cached = cache.get(number);
      let response;
      try {
        response = await fetchImpl(
          `${API}/repos/${repository}/pulls/${number}`,
          {
            headers: {
              Accept: "application/vnd.github+json",
              "User-Agent": "jomcgi-site-preview",
              "X-GitHub-Api-Version": "2022-11-28",
              ...(cached ? { "If-None-Match": cached.etag } : {}),
            },
            redirect: "error",
            signal: AbortSignal.timeout(30_000),
          },
        );
      } catch {
        throw new GitHubError(
          503,
          "GitHub did not answer the PR lookup; no preview URL was issued",
        );
      }
      if (response.status === 304 && cached) {
        cached.at = now();
        return cached.body;
      }
      if (response.status === 403 || response.status === 429) {
        const reset = Number(response.headers.get("x-ratelimit-reset"));
        const until =
          Number.isFinite(reset) && reset > 0
            ? ` until ${new Date(reset * 1000).toISOString()}`
            : "";
        throw new GitHubError(
          503,
          `GitHub refused the PR lookup (HTTP ${response.status}, anonymous rate limit${until}); no preview URL was issued, retry later`,
        );
      }
      if (response.status === 404) {
        throw new GitHubError(404, `PR #${number} does not exist`);
      }
      if (!response.ok) {
        throw new GitHubError(
          502,
          `GitHub PR lookup: HTTP ${response.status}; no preview URL was issued`,
        );
      }
      const body = await response.json();
      const etag = response.headers.get("etag");
      if (etag) cache.set(number, { etag, body, at: now() });
      return body;
    },
  };
}
