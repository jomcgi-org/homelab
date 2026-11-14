import { server } from "./index.js";
import { protectResponseCache } from "./http-cache-policy.js";

// A prepended request listener runs before Polka's generated handler. Keeping
// the generated server preserves adapter-node's graceful shutdown and socket
// activation. Headers are checked when written, after the final status is known.
server.server.prependListener("request", protectResponseCache);

export { server };
