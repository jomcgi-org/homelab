# Grimoire Live-Play: User Journeys and Requirements

**Date:** 2026-07-04
**Status:** Approved design (interactive brainstorm with Joe, 2026-07-04)
**Builds on:** ADR services/011 (hot-tier schema), ADR services/012 (Postgres-first), `docs/plans/2026-07-02-grimoire-pg-first-spec.md`, `docs/plans/2026-07-03-grimoire-ui-overhaul.md`

Grimoire has data flowing in: the Monster Manual corpus is ingested, extracted, embedded, and browsable on both tiers. This document defines the next program: turning Grimoire from a campaign reference tool into a live-play platform that removes the tedious parts of tabletop D&D. It captures the user journey stories for the three roles (Player, DM, Visitor) and the numbered requirements each journey implies.

## 1. Decisions made during the brainstorm

These were decided interactively and are fixed inputs to this design:

| # | Question | Decision |
|---|----------|----------|
| D1 | Who are the users? | Joe's real play group. Players get invited accounts; no open signup. Visitors stay on the read-only public tier. |
| D2 | Audio capture | A Discord bot joins the group's voice channel and receives per-user audio streams. Transcription runs in-cluster (Whisper on the inference node). |
| D3 | Automation depth | Theatre-of-the-mind engine: full mechanical automation (sheets, initiative, action resolution, dice, slots, inventory) with no battle maps or tokens. |
| D4 | Reveal control | Aggressive auto-surfacing. Both recall and plausible new reveals push to player screens automatically. The DM holds a kill switch, per-entity locks, and one-tap retract, not a pre-approval queue. |
| D5 | Visitor scope | Today's corpus demo plus a curated, sanitized session-replay showcase. No visitor accounts or interactivity. |
| D6 | Live-loop architecture | Monolith-embedded: Postgres as the event bus, SSE push to session screens, 5 to 15 second word-to-card latency. No new datastore, queue, or websocket gateway. |

## 2. Personas

- **Priya (Player):** one of 3 to 6 invited friends. Plays a cleric. Wants to play her character, not do arithmetic or flip through books. Takes patchy notes.
- **Joe (DM):** runs the campaign, authors homebrew, adjudicates rules, manages logistics. Wants prep to be reusable, secrets to stay secret until spoken, and total override authority at the table.
- **Visitor:** anonymous browser on jomcgi.dev, likely evaluating Joe's work. Wants to see something real and impressive in under two minutes.

## 3. Knowledge tiers

Four tiers govern what surfaces to whom. The first and fourth already exist in the schema; the second is partly built; the third is new.

| Tier | Definition | Mechanism |
|------|-----------|-----------|
| **Global knowledge** | World facts anyone in-setting would know | `entity.is_global = true` + corpus chunks (live today) |
| **Personal knowledge** | What this PC specifically knows: background, prior sessions, their own notes | Per-PC `knowledge_grant` (full / partial / name_only, live today) + PC background entities (new) + player notes (new) + transcript-derived facts granted to that PC (new) |
| **Non-player rules** | How the game operates. Distinctly surfaced, never mixed with lore. At DM discretion but easy to see. | Chunks tagged `content_kind = 'rules'`, rendered in a dedicated rules lane visible to the whole table, with a per-campaign DM toggle (new) |
| **Campaign info** | The DM's material: plots, homebrew, unrevealed NPCs | `entity.is_global = false`, invisible to players until granted (live today) |

**Reconciling D4 (aggressive auto-reveal) with DM-only campaign info:** when the DM's own voice introduces a campaign entity at the table, the pipeline auto-grants it to the present PCs at `name_only` or `partial` scope. The DM saying it aloud is the reveal; the platform just does the bookkeeping. Safeguards:

1. Any entity can be **locked**: it never auto-reveals regardless of what the transcript says, and only a manual grant exposes it.
2. Every automatic action lands in the DM's live ledger with a one-tap **retract** that revokes the grant and pulls the card from player screens.
3. Player speech never creates new grants. Players talking about "Strahd" surfaces only what they already know; the DM's speech is the only reveal channel.
4. A per-campaign confidence threshold gates auto-grants; below it, the suggestion sits in the DM ledger unactioned.

## 4. User journey stories

Each story lists the requirements it depends on. Requirement IDs are defined in section 5.

### J-P1: Priya joins and creates her character

Priya receives an invite link from Joe. She logs in (Cloudflare Access, her email invited by Joe) and lands on the campaign's welcome page. A guided 5e builder walks her through species, class, background, ability scores, skills, and starting equipment, validating each step against the rules corpus (point-buy totals, class proficiency choices, starting gold). The finished sheet is the mechanical source of truth for everything downstream: rolls, slots, inventory, encumbrance.

Her background ("acolyte of Kelemvor, grew up in Baldur's Gate") creates personal-knowledge entities and grants that only her PC holds. From day one, the platform knows what Priya's character would know.

*Requires: FR-ID-1..5, FR-CHAR-1..4, FR-SURF-6*

### J-P2: Priya prepares between sessions

Midweek, Priya opens Grimoire. The recap of last session is on the campaign home. Her notes page shows a generated draft ("Session 7, from Mira's perspective") built on top of her own jottings; she edits it, deletes a wrong guess, stars the sewer-entrance detail. She browses the library and entity index, seeing exactly the global corpus plus what Mira has been granted. She swaps two prepared spells and buys rations from the party's pooled gold, and her sheet updates.

*Requires: FR-NOTE-1..4, FR-GEN-2, FR-CHAR-5, FR-INV-1..3, existing grant-filtered library*

### J-P3: Priya plays a live session

Session night. Priya joins the Discord voice channel; the bot is already there because Joe started the session. She opens the session screen on her laptop.

As the table talks, her **Known to you** lane populates: when the innkeeper NPC from session 2 comes up, his card surfaces with what Mira knows; when someone mentions the sewers, her own starred note appears. When the fighter asks "wait, can I shove him off the bridge?", the **Rules** lane surfaces the shove rules for the whole table, visually distinct from lore.

Joe sends her a private Discord DM: "the symbol on the door is Kelemvor's, you recognize it instantly." The surfacing pipeline runs on the whisper too; a private card with Kelemvor's entity appears on her screen only.

Combat starts. Initiative auto-rolls from sheets (Joe can reorder). On her turn, her action card shows her options with everything pre-computed: attack rolls with her modifiers, spell save DC, remaining slots. She taps Sacred Flame on the ghoul; the platform rolls, applies the ghoul's save, deducts damage, and logs it. Her holy water count decrements when she uses it. Nobody at the table does arithmetic.

*Requires: FR-LIVE-1..6, FR-SURF-1..5, FR-COMBAT-1..7, FR-INV-2*

### J-P4: Priya after the session

The next morning, Priya's notes page has a new draft appended: what happened in session 8 from Mira's perspective, built from the transcript, seeded with her existing notes and phrasing. She accepts most of it, tweaks a line. The entities revealed last night (the pale count, the village of Barovia) now appear in her entity index with the scope Joe's speech granted.

*Requires: FR-GEN-1..3, FR-SURF-4*

### J-DM1: Joe preps the campaign

Joe creates the campaign and invites players by email. He authors campaign entities: NPCs, locations, factions, plot devices. All are DM-only (`is_global = false`) by default. He marks the campaign's central twist entity **locked** so no transcript can ever auto-reveal it. He builds Friday's encounter by pulling three ghouls and a ghast from the Monster Manual entities into an encounter list, sees the computed difficulty for the party's level, and attaches a session outline note.

*Requires: FR-ID-2..3, FR-DM-1..3, FR-COMBAT-8, existing entity/grant CRUD*

### J-DM2: Session zero and logistics

Joe schedules session zero. Players create characters (J-P1); Joe reviews each sheet, approves them, or bounces one back with a comment ("we rolled stats, not point-buy"). He sets campaign config: rules lane on, auto-reveal confidence threshold, which books are in-setting.

*Requires: FR-CHAR-6, FR-DM-4, FR-SURF-5*

### J-DM3: Joe runs a session

Joe hits **Start session**. The bot joins voice, transcription begins, and the god view opens: a rolling transcript with speaker attribution, every player's surfacing lanes, the auto-action ledger, and the encounter runner.

He narrates the party's arrival; when he names the village, the ledger shows "auto-granted: Barovia (partial) to 4 PCs" and cards land on player screens. A ghoul fight starts: he launches the prepared encounter, initiative merges monsters and PCs, and on each monster turn he picks an action and a target and the engine resolves it. The rogue tries something weird; Joe overrides the suggested ruling and types a one-line judgement, which the table sees and the log keeps.

Halfway through, he notices the pipeline surfaced a partial card for an NPC he wanted mysterious. One tap: retracted, card gone from screens, entity locked. Play never stops. When the internet hiccups and transcription drops for a minute, the session screen says so and everything else keeps working; the lanes just go quiet until audio returns.

*Requires: FR-LIVE-1..6, FR-SURF-1..5, FR-COMBAT-1..8, FR-DM-5..6, NFR-6*

### J-DM4: Joe wraps up and publishes

Joe ends the session. The pipeline drafts: a table-wide recap, per-player note drafts, the grants ledger (what got revealed, to whom, at what scope), and XP/loot deltas. He reviews, fixes one attribution, and publishes the recap to the campaign.

Once a campaign arc concludes, Joe optionally runs the showcase flow: pick a session, review the sanitization pass (player names pseudonymized, out-of-game chatter stripped, only SRD-licensed rules content retained), and publish a replay to the public tier.

*Requires: FR-GEN-1..4, FR-PUB-1..3*

### J-V1: A visitor browses the corpus

Unchanged from today: library, book reader, entity index with stat blocks, search over `is_global` content, Turnstile-gated and noindexed.

*Requires: existing public tier*

### J-V2: A visitor watches a session replay

From the Grimoire demo page, the visitor opens **Watch a real session**. A replay page plays back a sanitized session: transcript excerpts scroll, knowledge cards surface in time with the conversation they responded to, the combat log ticks through a fight. Scrub bar, no audio, no login. It is the two-minute proof that the live pipeline is real.

*Requires: FR-PUB-1..3*

## 5. Functional requirements

### Identity and access (FR-ID)

- **FR-ID-1:** Player authentication reuses Cloudflare Access; the DM invites a player by adding their email. No password or signup system is built.
- **FR-ID-2:** A `grimoire.app_user` record maps authenticated email to identity; `campaign_member` maps user to campaign with `role in (dm, player)` and an optional `player_character_id`.
- **FR-ID-3:** Viewpoint is derived from authenticated identity. The `?as=` query parameter is removed from the private tier.
- **FR-ID-4:** Each user can link exactly one Discord user ID, used for voice attribution and whisper ingestion.
- **FR-ID-5:** All grimoire private endpoints enforce campaign membership; DM-only endpoints (grants, entity authoring, session control, overrides) enforce `role = dm`.

### Character management (FR-CHAR)

- **FR-CHAR-1:** Guided character builder covering species, class, background, abilities (point-buy, standard array, or manual), skills, and starting equipment, validated against the rules corpus.
- **FR-CHAR-2:** The sheet is the single mechanical source of truth: ability mods, proficiency, AC, HP, save DCs, attack bonuses, spell slots are computed, not typed.
- **FR-CHAR-3:** Character background text generates personal-knowledge entities and grants scoped to that PC.
- **FR-CHAR-4:** Sheet changes are versioned with a visible history (who changed what, when, in which session).
- **FR-CHAR-5:** Leveling, rests (short/long), and preparation changes update derived stats and slots automatically.
- **FR-CHAR-6:** DM approval flow: a sheet is `draft` until the DM approves it into play; the DM can return it with a comment.

### Player notes (FR-NOTE)

- **FR-NOTE-1:** Players have per-PC notes (create, edit, delete, star) stored in grimoire, linkable to entities.
- **FR-NOTE-2:** Notes are personal-knowledge inputs: embedded and surfaced back to their owner in live sessions and search.
- **FR-NOTE-3:** Notes are DM-readable by default (it feeds surfacing quality and DM awareness) with a per-note private flag the DM cannot read.
- **FR-NOTE-4:** Generated drafts (FR-GEN-2) never overwrite player text; they append as drafts the player accepts, edits, or discards.

### Live session infrastructure (FR-LIVE)

- **FR-LIVE-1:** Session lifecycle (create, start, pause, end) drives everything: the bot joins on start and leaves on end; utterances, grants, combat events, and notes drafts are all session-keyed. Single-active-session invariant per campaign stands.
- **FR-LIVE-2:** A Discord bot joins the configured voice channel and captures per-user audio streams; the user-to-PC mapping comes from FR-ID-4.
- **FR-LIVE-3:** Transcription runs in-cluster (Whisper-class model on the inference node); utterances persist to `grimoire.session_utterance` (session, speaker user/PC, text, timestamps) with `embeddable_kind = 'transcript'` embeddings.
- **FR-LIVE-4:** Private Discord DMs from the DM to a linked player during an active session are ingested as whisper utterances, visible and surfaced only to that player (and the DM).
- **FR-LIVE-5:** Session screens receive updates over SSE from the monolith; Postgres is the only bus (D6).
- **FR-LIVE-6:** The transcript is retained per session and browsable afterward by campaign members (players see table talk plus their own whispers; the DM sees all).

### Real-time knowledge surfacing (FR-SURF)

- **FR-SURF-1:** New utterances are embedded and matched (kNN over the existing `embedding` table plus entity-name matching) against entities, chunks, rules content, and the speaker-visible notes; an in-cluster LLM judge scores relevance and decides the lane.
- **FR-SURF-2:** Three lanes on the session screen: **Known to you** (per-PC recall: granted entities, global lore, own notes), **Rules** (table-wide, visually distinct), **Revealed** (new grants landing live).
- **FR-SURF-3:** Auto-reveal policy per D4: DM speech (voice or whisper) can auto-grant campaign entities at `name_only`/`partial` scope to present PCs above the campaign confidence threshold; player speech only ever triggers recall.
- **FR-SURF-4:** Every auto-action is written to a session ledger with actor "pipeline", the triggering utterance, and a one-tap retract that revokes the grant and removes the card from player screens.
- **FR-SURF-5:** DM controls: per-entity **lock** (never auto-reveal), per-campaign kill switch (surfacing off), rules-lane toggle, confidence threshold.
- **FR-SURF-6:** Recall cards deep-link into the entity/chunk/note they surface; scope chips show why the viewer can see it.

### Rules content (FR-RULE)

- **FR-RULE-1:** Chunks carry a `content_kind` (`lore` | `rules`); the rules lane and rules-validated flows (builder, combat) read only `rules` content.
- **FR-RULE-2:** The SRD 5.2 (CC-BY-4.0) is ingested as the canonical rules corpus, so rules content is licensable on the public showcase; full WotC books remain private-tier lore.

### Combat and action engine (FR-COMBAT)

- **FR-COMBAT-1:** Initiative: auto-rolled from sheets and monster stat blocks, merged into a turn tracker; the DM can reorder, insert, and remove.
- **FR-COMBAT-2:** Turn cards: on a PC's turn the player sees their legal actions (attacks, prepared spells with slot state, items, class features) with modifiers, DCs, and ranges pre-computed from the sheet.
- **FR-COMBAT-3:** Resolution: attack rolls, saves, checks, damage, and conditions resolve platform-side (advantage/disadvantage, crits, resistances from stat blocks); results append to a combat log every participant sees.
- **FR-COMBAT-4:** Monster turns: the DM picks action and target from the entity's stat block (already extracted into `entity_creature`); the engine resolves.
- **FR-COMBAT-5:** HP, temp HP, conditions, concentration, death saves, and spell-slot state tracked per combatant, visible per ACL (players see their own detail plus table-visible state the DM chooses to show).
- **FR-COMBAT-6:** DM override on everything: any roll result, ruling, HP value, or condition can be set manually; overrides are logged as judgements the table can see.
- **FR-COMBAT-7:** Consumables decrement on use (ammo, potions, components with cost) via FR-INV.
- **FR-COMBAT-8:** Encounter builder: compose encounters from corpus/homebrew creatures ahead of time with party-relative difficulty; launch into a session with one action.

### Inventory and logistics (FR-INV)

- **FR-INV-1:** Per-PC inventory (items, quantities, attunement, container/carried state) plus a party pool (gold, shared loot); encumbrance computed from the rules corpus.
- **FR-INV-2:** Inventory mutations from play (loot award, purchase, consumption, trade between PCs) are session-logged and reflected on sheets immediately.
- **FR-INV-3:** Currency arithmetic (split the loot, make change across denominations) is platform-side.

### Generated artifacts (FR-GEN)

- **FR-GEN-1:** On session end, the pipeline drafts a table-wide recap from the transcript and ledgers, for DM review before publishing to the campaign.
- **FR-GEN-2:** Per-player note drafts are generated from the transcript restricted to that PC's knowledge (their utterances, table talk, their whispers, their grants), seeded with the player's own note style and content (D: notes built on their content as the base).
- **FR-GEN-3:** Session ledgers are first-class outputs: grants (what was revealed to whom), combat log, XP, loot. The DM can amend before they finalize.
- **FR-GEN-4:** All generation runs on in-cluster models; a generation failure degrades to "no draft" and never blocks session close.

### DM authoring and control (FR-DM)

- **FR-DM-1:** Campaign entity authoring UI: create/edit homebrew entities and relationships (typed details included), DM-only by default.
- **FR-DM-2:** Session outlines: the DM can attach prep notes and planned entities/encounters to a future session; prep is surfaced to the DM (never players) during that session.
- **FR-DM-3:** Locked-entity management: mark/unmark entities as never-auto-reveal (FR-SURF-5).
- **FR-DM-4:** Campaign settings: rules lane toggle, confidence threshold, in-setting book list, Discord voice channel binding.
- **FR-DM-5:** God view session screen: full transcript, all players' lanes, ledger with retract, encounter runner, override console.
- **FR-DM-6:** Manual grant/reveal remains first-class (existing grant CRUD) alongside auto-reveal; the ledger shows both uniformly.

### Public showcase (FR-PUB)

- **FR-PUB-1:** A showcase is an explicit DM-triggered export of one session: pseudonymize players/PCs on request, strip flagged utterances, and include rules content only from the SRD corpus (FR-RULE-2). Nothing session-related is public by default.
- **FR-PUB-2:** The sanitization output is a static replay document reviewed by the DM before publishing; publishing copies it to public-tier storage. Retraction removes it.
- **FR-PUB-3:** The public replay page plays back transcript excerpts, surfaced cards, and the combat log on a scrub bar; read-only, no accounts, Turnstile-gated and noindexed like the rest of the Grimoire demo.

## 6. Non-functional requirements

- **NFR-1 Latency:** word-to-card p50 under 10 seconds, p95 under 20 seconds (D6 architecture). Combat interactions (tap to resolved) under 1 second.
- **NFR-2 Privacy:** raw audio is discarded after transcription; transcripts are retained. Friends' voices are PII; transcripts are the durable record. Whispers are never visible beyond sender and recipient.
- **NFR-3 Cost:** the live loop uses only in-cluster models (Whisper-class ASR, Qwen judge/generation, voyage-4-nano embeddings). No per-token external spend during sessions.
- **NFR-4 Copyright:** WotC book content never appears on the public tier beyond the existing noindexed demo posture; showcases carry SRD rules content only.
- **NFR-5 Auditability:** every state change at the table (roll, grant, retract, override, inventory mutation) is an immutable session-keyed log row.
- **NFR-6 Degradation:** the session survives bot, ASR, or judge outages; play continues with lanes quiet and combat/inventory unaffected. Recovery resumes surfacing without restart.
- **NFR-7 ACL enforcement depth:** knowledge-tier filtering is enforced in backend query predicates (as today via `visible_entities_query`), never frontend-only.

## 7. ACL matrix

| Capability | DM | Player | Visitor |
|---|---|---|---|
| Campaign entities (ungranted) | full | none | none |
| Granted entities | full, plus grant ledger | per grant scope (full / partial / name_only) | none |
| Global corpus (books, entities, search) | full | full | `is_global` only |
| Rules lane / rules corpus | full, plus toggle | read | showcase replay only |
| Own PC sheet, notes, inventory | read/write all PCs | own only (DM-readable notes unless flagged private) | none |
| Other PCs' sheets and notes | read | none | none |
| Live transcript | all utterances and whispers | table talk plus own whispers | sanitized showcase only |
| Combat state | override anything | own turn actions, own detail, table-visible state | showcase replay only |
| Grants and reveals | create, retract, lock, configure | receive | none |
| Session lifecycle, campaign config | full | none | none |

## 8. Phasing

Each phase is independently shippable and valuable; order of 4 and 5 can swap on appetite.

1. **Identity and ACL hardening:** app_user/campaign_member, Cloudflare Access wiring, retire `?as=`, role-enforced endpoints. Everything depends on this. *(FR-ID)*
2. **Character builder and notes:** sheets as source of truth, background knowledge, player notes. No live dependency. *(FR-CHAR, FR-NOTE, FR-INV-1)*
3. **Session lifecycle, Discord bot, transcription:** live transcript on screen with attribution; no intelligence yet. *(FR-LIVE)*
4. **Surfacing lanes and auto-reveal:** the headline magic; rules lane requires the SRD ingest. *(FR-SURF, FR-RULE)*
5. **Combat engine and inventory in play:** turn cards, resolution, encounter runner. Independent of 3 and 4. *(FR-COMBAT, FR-INV-2..3)*
6. **Generated artifacts and public showcase.** *(FR-GEN, FR-PUB)*

## 9. Out of scope

- Battle maps, tokens, fog of war, or any VTT-style spatial play (D3).
- Open signup, multi-tenant campaigns, or any users beyond the invited group (D1).
- Platform-native audio capture (D2; Discord is the capture surface).
- Real-time captions or sub-2-second surfacing (D6; Option B explicitly rejected).
- Visitor interactivity beyond the replay (D5; sandbox rejected for abuse/cost surface).
- Ruleset coverage beyond D&D 5e.

## 10. Positions taken (flag in review if wrong)

- **Audio retention:** transcribe then discard; storing friends' voice audio buys little and costs privacy.
- **Rules corpus:** SRD 5.2 (CC-BY-4.0) is the rules lane source; full books stay private lore.
- **Note visibility default:** DM-readable with per-note private opt-out.
- **Auto-reveal scope ceiling:** DM speech auto-grants at `partial` max; `full` grants stay manual.
