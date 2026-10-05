# Platform enrollment and access

Status: proposed implementation of Joe's 2026-10-05 direction, tracked on
[#6858](https://github.com/jomcgi-org/homelab/issues/6858). This document records
the intended boundary. The platform enrollment API, permission registry and MCP
tools described here are not implemented or enabled.

An invitation to the platform permits account creation. An invitation to a game
permits joining that game. Creating an account grants no application permission
or campaign membership.

Authentik owns credentials, signup and sign-in. The platform owns its user
records, invitation lifecycle and application permissions. A user is linked to
the verified Authentik issuer and subject; email is contact information and an
invitation recipient constraint, not the identity key.

**Why.** Authentik's invitation creation permission accepts other flows and
arbitrary fixed data. Joe rejected that boundary. Platform enrollment must not
hold a credential with Authentik invitation, user, group, provider or permission
management authority. Keeping enrollment in the shared auth module also avoids
putting account creation policy inside Grimoire or creating another service.

## Shared service boundary

Extend `projects/monolith/auth` with platform account, invitation and grant
commands. Keep the existing token verifiers and `Principal` API compatible.
HTTP handlers, the enrollment adapter and MCP tools call the same commands;
transport-specific code cannot bypass their authorization, state transitions or
audit writes.

Use platform-owned storage for:

- Users: stable platform ID, active/disabled state and display/contact fields.
- Identities: unique issuer and subject linked to one platform user. Linking an
  additional identity requires an explicit authorized operation; matching email
  never merges accounts.
- Invitations: recipient, digest of a random capability, issuer, expiry,
  revocation state and accepted identity. Raw capabilities are never recoverable
  from storage. Reissuing rotates the capability and invalidates the old one.
- Grants: platform user, registered application permission and issuing actor.
- Audit records: authenticated actor, action, target, request ID, timestamp and
  result. Passwords, tokens, invitation capabilities and signup payloads are
  excluded from logs, traces and audit bodies.

Grimoire keeps its app user IDs and campaign foreign keys. An explicit link to
the platform user replaces independent account enrollment; migrating existing
identities must preserve membership and reject ambiguous email-only records.
Existing operator accounts need an explicit bootstrap path. A normal first
login cannot bootstrap management authority or bypass platform enrollment.

## Invitation-required signup

Platform invitations are administrator-issued. Open registration is out of
scope. The user opens a platform invitation, completes Authentik enrollment and
returns with a verified identity. Platform activation binds that identity and
consumes the platform invitation atomically with creating the platform record.
The enrolled identity initially receives no groups, Authentik roles, platform
administration or application grants.

The Authentik enrollment adapter must validate the invitation before user
creation and bind the fixed recipient to the completed identity. It may use a
capability scoped to one platform invitation, with a fixed action and audience;
it must not use Authentik's native invitation-creation API. The precise completion
proof remains an implementation gate: a browser-provided success flag, email
claim alone or arbitrary user-editable attribute is insufficient. The platform
must verify the signed identity and trusted enrollment binding, then recheck
expiry and revocation when committing activation.

Signup interruption is recoverable without transferring the invitation to a
different identity. Concurrent attempts must create at most one platform user
and must not overwrite an existing Authentik account. If revocation races with
an enrollment already past validation, an Authentik account may finish creation;
the revoked invitation still cannot activate a platform user or application
access. Cleanup and retry ownership must be explicit.

Email-bound signup needs mailbox confirmation or an equivalent trusted
verification mechanism. Live Authentik SMTP configuration was absent when
reviewed on 2026-10-05; email verification is not claimed ready.

## Permissions after enrollment

Applications register named permissions instead of accepting arbitrary group,
role or scope strings. Start with `grimoire.access` and
`grimoire.create_game`. Access does not imply game creation, ownership, DM status
or membership. Campaign invitation and Join commands continue to own membership.
A campaign link is consumed only when authenticated Join commits successfully,
independently of the earlier platform invitation.

The gateway retains Authentik OIDC protection for the Grimoire surface, with the
existing exact campaign landing exception. The application checks active
platform status and `grimoire.access` on protected Grimoire operations. A signed
token by itself cannot bypass disabled-user or revoked-grant checks. The UI
reflects the same grant that the backend enforces for creating games.

Permission management initially requires the existing standing human
`operators` authorization. Application grants cannot grant platform management
authority, Authentik groups, MCP access or permissions for Moving. New enrollment
must not inherit those privileges. Disabling a platform user blocks the apps
participating in this module; it does not claim to disable the Authentik account,
revoke an OIDC session or change independent applications.

## HTTP and MCP share commands

The private management API uses `/api/auth/platform` and the shared private MCP
server registers the tools below. These are proposed contracts, not currently
callable endpoints. They are absent from the public and monolith-agents tiers.
Normal users use a separate authenticated self endpoint for their own profile
and grants; it exposes no user directory or mutation of permissions.

| MCP tool | Shared operation | Result |
| --- | --- | --- |
| `platform_invitation_issue` | Issue recipient-bound invitation with bounded expiry and idempotency key | Invitation ID, status, expiry and authenticated delivery reference |
| `platform_invitation_list` | List bounded, filtered invitation metadata | Paginated metadata without capabilities |
| `platform_invitation_revoke` | Revoke an exact invitation | Current status; repeated revoke is harmless |
| `platform_user_list` | List bounded platform users | Paginated account summaries without credentials |
| `platform_user_get` | Read one stable platform user ID | Status, identity references and application grants |
| `platform_user_set_active` | Enable or disable one platform user with a reason | Current platform status and affected application access |
| `platform_permission_list` | Read registered application permission definitions | Names and descriptions, without Authentik administration permissions |
| `platform_user_grant` | Grant one registered application permission to an exact platform user | Current grant and audit reference |
| `platform_user_revoke` | Revoke one exact application grant | Current state and audit reference |

Every command checks a verified standing human operator, including MCP calls
that pass the shared coarse group gate. Caller-supplied actor fields, tool
visibility and network access supply no authority. Disabled platform actors
cannot administer other users. Mutations require an explicit target, reason and
bounded idempotency key; state changes and their audit entries commit together.
User and invitation searches are bounded and return only management metadata.

Issuing through MCP never returns a usable invitation link. Delivery uses an
approved email path or an authenticated operator page which displays the raw
link once. The reference returned by MCP authorizes neither signup nor viewing
the capability. No tool accepts or returns a password, Authentik bearer, OIDC
client secret, arbitrary provider operation or group-management request.

**Why.** The same management commands are useful from the UI, HTTP automation
and MCP. Sharing command authorization prevents the MCP surface from becoming a
second, more privileged account-management path. Keeping delivery separate also
prevents a tool response from depositing a signup credential in chat history.

## Repository and activation boundary

Implementation proceeds behind separate default-off enrollment and management
gates. A migration or module import must not expose management tools, provision
credentials or change current application authorization. Identity migration,
new grants for existing users, creation-policy changes and their rollout need
an explicit deployment review. Main publishing still makes a merge a production
deployment even when a feature is disabled.

The acceptance record stays on #6858. It must cover HTTP/MCP authorization
parity, anonymous/workload/ordinary-user denial, target substitution,
self-escalation denial, invitation expiry/revoke/replay/concurrency,
interrupted signup, identity binding, grant removal and disabled-user behavior
with an existing session. Real gateway/Auth/OIDC evidence must verify new and
existing players, registration resumption, campaign Join and no access expansion
to Moving, other Authentik flows or the agents MCP tier.

Current deployed behavior remains separate: #6860 enabled registered-player
campaign links on monolith chart `0.647.0`; new-account enrollment remains off.
`grimoire.AppUser` already links issuer and subject, while creating a campaign
currently requires authentication rather than an explicit creation grant. This
document does not claim those new platform checks have shipped.
