# Scotland trips

A plugin for Claude Code and Codex that connects the
[Scotland connector](https://jomcgi.dev/scotland): a free, read-only MCP
server that plans and checks Scottish walks and stargazing. It answers which
Walkhighlands walks fit the weather and where the night sky will be dark and
clear, each with a go, marginal or no verdict and its reason.

The plugin is the MCP server's URL plus a skill that tells the assistant
when to use `plan` and when to use `check`, and how to turn "no rain at
all" into a weather limit. There is no account or key.

## Install

Claude Code:

```sh
/plugin marketplace add jomcgi/scotland-plugin
/plugin install scotland@jomcgi
```

Without the plugin, add the server alone:

```sh
claude mcp add --transport http scotland https://jomcgi.dev/scotland/mcp
```

Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.scotland]
url = "https://jomcgi.dev/scotland/mcp"
```

## Layout

| File                              | For                                                               |
| --------------------------------- | ----------------------------------------------------------------- |
| `.claude-plugin/plugin.json`      | Claude Code plugin manifest                                       |
| `.claude-plugin/marketplace.json` | this repository as a one-plugin marketplace                       |
| `.mcp.json`                       | the MCP server, for Claude Code                                   |
| `plugin.json`, `mcp.json`         | the same plugin for OpenAI's plugin directory (Codex and ChatGPT) |
| `skills/scotland-trips/SKILL.md`  | when and how to use the tools, shared by both                     |

Raise `version` in both manifests together for each release; installed
copies use it to tell that an update exists. The tools themselves change on
the server and need no plugin release.

## Links

[Privacy](https://jomcgi.dev/scotland/privacy) ·
[terms](https://jomcgi.dev/scotland/terms) ·
[support](https://jomcgi.dev/scotland/support) ·
[method](https://jomcgi.dev/scotland/v1/method) ·
[OpenAPI](https://jomcgi.dev/scotland/v1/openapi.json)

## Licence

MPL-2.0, as the homelab repository it is staged in. See `LICENSE`.
