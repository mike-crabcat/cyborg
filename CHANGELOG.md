# Changelog

All notable changes to Cyborg are documented here. Entries are based on analysis of actual code changes, not just commit messages.

## 2026-06-09

### Added
- Add tool-based reconciliation loop replacing the previous JSON-operations approach: the LLM now has `get_entity`, `list_entities`, `add_claim`, `retract_claim`, `supersede_claim`, `create_entity`, `delete_entity`, and `merge_entities` tools to inspect and fix entities directly
- Add orphan connection linking rules to reconciliation: connections without a parent trip are automatically discovered and linked by the LLM using entity read tools
- Add `gpt-5.5` pricing to the dashboard cost tracker

### Changed
- Switch reconciliation from `memory_model` (small model) to the default bigger model for more capable autonomous entity repair
- Change connection entity extraction from one-entity-per-booking to one-entity-per-hop: multi-leg journeys under a single PNR are now separate connection entities with shared `booking_ref`
- Add reconciliation rule for stays to enforce exactly one arrival and one departure date, retracting duplicates

### Removed
- Remove inline `_apply_operations()` reconciliation dispatcher, replaced by tool-based LLM loop

## 2026-06-08

### Added
- Add routines system: cron-scheduled prompts injected into sessions via `read_routine`/`write_routine`/`delete_routine` agent tools, with a `RoutineSchedulerTask` heartbeat task that fires due routines independently without blocking session activity
- Add entity merge system for detecting and merging duplicate entities using embedding cosine similarity and LLM confirmation, with a CLI command (`cyborg memory merge --dry-run`), a dashboard API endpoint, and an inline merge UI in the memory dashboard
- Add centralized entity type registry (`ENTITY_TYPE_REGISTRY` in `claim_types.py`) consolidating per-type metadata (prefixes, descriptions, keywords, extraction rules, reconciliation rules, display behavior)
- Add `connection` as a first-class entity type replacing the old `transport` type, with structured claim types for departure/arrival locations, times, transport type, duration, booking ref, route, passengers, and seat
- Add `stay` entity type replacing `tripstop`, with renamed claim keys (`accommodation`, `arrival_date`, `departure_date`, `accommodation_type`, `accommodation_address`)
- Add new claim types: `preference` (person), `interest`/`opening_hours` (location), `attraction` (trip), `booking_ref`/`route`/`passenger`/`seat` (connection)
- Add `find_session` and `search_bulletins` agent tools for discovering sessions by name and searching memory bulletins by time horizon with trust-scoped access control
- Add `list_attachments` and `download_attachment` email tools for browsing and saving email attachments, with attachment metadata persisted to the database for all messages
- Add Jinja2-based entity template engine for rich entity rendering with recursive entity reference resolution
- Add entity deprecation status (`active`/`archived`/`deprecated`) and automatic deprecation of file entities with no valid `file_path` claim
- Add `cyborg memory reindex` CLI command for rebuilding the FTS search index without LLM calls
- Add PDF file viewer in the workspace dashboard using an embedded iframe
- Add `deprecated` entity status to the database schema and migrations (schemas 325–328)

### Changed
- Convert entity rendering to async across all call sites to support recursive entity reference resolution via database lookups
- Improve supplement prompt to prevent cross-entity claim extraction: claims must be about the target entity, not about other entities mentioned in the bulletins
- Persist source bulletin IDs on supplement-generated claims instead of leaving them empty
- Skip all entity-ref claims during supplement to prevent inferred relationships
- Remove self-referential claims created during entity merges
- Change entity ID display in memory dashboard from CSS-truncated to word-wrapped full IDs
- Switch datetime handling in routines and cron to use local timezone instead of UTC
- Handle `CancelledError` gracefully in LLM dispatch: catch `BaseException` and log cancellations as "server restart" instead of generic errors
- Store attachment metadata for all email messages on receipt, then auto-download for trusted senders
- Merge actual email reply body text into assistant session messages (matching WhatsApp behavior)
- Move email attachment downloads from `projects_base_dir` to `data_dir`

### Fixed
- Fix supplement producing text-value connection claims instead of entity references by skipping all entity-ref claim types during supplement extraction
- Fix embedding upsert to use DELETE+INSERT instead of INSERT OR REPLACE to handle stale data in sqlite-vec

### Removed
- Remove orphan transport discovery (`_find_orphan_transports`) from reconciliation, replaced by per-connection orphan linking rules

## 2026-06-07

### Added
- Add `POST /api/v1/email/poll` endpoint for on-demand email inbox polling with `force=True` to bypass the interval check
- Add `/email` slash command skill for triggering immediate email inbox check
- Add entity reconciliation system with LLM-driven consistency checking, per-type rules, and human-in-the-loop conflict resolution via questions
- Add `supplement_entity` pipeline to gap-fill missing claims from related bulletins after dream processing
- Add `memory_correct` tool supporting `remove_entity`, `remove_claim`, and `set_truth` actions for agent-driven memory correction
- Add `truth` claim type for user-stated facts and corrections that override inference, with migration of legacy `purpose` answer claims
- Add `memory reconcile` and `memory supplement` CLI commands for manual entity consistency repair and gap-filling
- Add memory questions API endpoints and QA tab in dashboard for surfacing and resolving open reconciliation questions
- Add stats tab to memory dashboard showing entity distribution, pipeline status, and claim counts
- Add `rm`, `mv`, and `find` workspace tools for file deletion, move/rename, and content search
- Add `LLMCallStalenessTask` heartbeat to detect and mark LLM calls stuck in "running" status after 30 minutes
- Add orphan claims rendering in entity detail display for claims not covered by the entity template
- Add tool args and output display to live tool call cards in session call detail view
- Add `on_iteration_complete` callback to OpenAI tool-call loops for persisting intermediate messages to LLM call logs

### Changed
- Trigger memory dream immediately on bulletin write with 2-second debounce instead of waiting for the heartbeat cycle
- Run supplement and reconciliation automatically after each dream cycle for all touched entities
- Add pagination to email inbox polling for handling >50 unread messages
- Replace `list_files` workspace tool with `ls` (non-recursive, single-directory listing)
- Replace memory "lint" button with the new QA questions workflow
- Increase claim extraction `max_tokens` from 2000 to 4000
- Simplify trip entity model: remove destination/date claims at trip level, rely on tripstop-level data instead
- Strengthen transport entity extraction rules: require transport entities for all flights/trains/buses with route details
- Add explicit timeouts (300s read, 30s connect) to OpenAI client
- Include tool args and output summary in `llm.call.tool_completed` WebSocket events

### Fixed
- Fix backfilled emails never dispatched: poll now re-dispatches messages that were imported via sync but never reached the LLM
- Fix emails stuck unread in AgentMail: deduplicated messages are now marked as read even when processing is skipped

### Removed
- Remove dream trigger from `SessionIdleSummaryTask` heartbeat (moved to debounced bulletin-write trigger)
- Remove `/api/memory/lint` endpoint and its corresponding UI button

## 2026-06-06

### Added
- Add embedding-based semantic search using OpenAI text-embedding-3-small and sqlite-vec: entities are embedded at write time and queries like "what type of car does david have" now find results via cosine similarity when FTS5 keyword search fails
- Add bulletin detail page at `/memory/bulletins/{id}` showing source session/type, full text, and all claims extracted from that bulletin
- Add person entity rendering on contact detail page with rendered body display and "view in memory" link
- Add claim type registry (`claim_types.py`) with type-specific render templates that generate human-readable entity views from claims
- Add `render_entity()` function that deterministically renders entity claims into structured text using per-type templates (person, group, event, trip, etc.)
- Add FTS5 index built from rendered entity templates instead of raw claim data, improving keyword search relevance
- Add `rebuild_embeddings()` method for batch embedding all entities and `sqlite-vec` extension loading in database connection pool
- Add schema migrations 314–322: FTS5 index, claim types registry, claims v2 (claim_type_key replacing type/predicate/body), entity type renames, template-based FTS, and embedding vectors table
- Add file_path validation during claim extraction: file entities without a file_path claim are dropped automatically

### Changed
- Rename entity types: contact→person (slug-based IDs like `person-mike-cleaver`), artifact→file (requires file_path) and thing (physical objects with thing_type)
- Replace entity body documents with claim-only model: entities have no body column, all content is derived from claims via render templates
- Replace LLM-powered entity update with deterministic claim extraction: claims are the source of truth, entity views are generated on demand
- Add name-slug fallback for contact-to-person entity lookup when contact_id claim is missing
- Pre-map `{{contact:HEX8|Name}}` tags to `{{person-slug|Name}}` before LLM extraction so the LLM never sees raw contact IDs
- Update dashboard search to try embedding similarity when FTS5 returns no results
- Update memory recall tool with hybrid retrieval: exact ID → alias → embedding similarity → FTS5
- Update memory documentation to reflect current architecture

### Removed
- Remove social_relation claim type from registry, templates, and database (LLM over-generated it, producing noise)

## 2026-06-05

### Added
- Add patience system for LLM-driven message batching with WhatsApp typing awareness: buffers incoming messages, subscribes to contact presence, and dispatches only when the user stops typing
- Add ContactDirectory service for loading and querying contacts DB with `as_known_entities()` for bulletin generator context
- Add contact ID reconciliation for mapping non-canonical contact IDs (name slugs, unresolved variants) to canonical UUIDs during entity updates
- Add entity cleanup pipeline: merge duplicate contact entities, build renaming maps, rewrite claims/bulletins/entity relations, and run orchestrated cleanup via CLI
- Add email thread tools (`email_thread_read`, `email_thread_search`) for LLM function calling with trust-scoped access control
- Add email memory seeding from email thread history with seed_email CLI command
- Add phone call result service for surfacing call outcomes back to originating sessions
- Add email thread result tools for surfacing email outcomes back to originating sessions
- Add generic thread result service for linking communication threads to their origin sessions
- Add contact entity and claims API endpoints (dashboard and REST) for viewing memory data per contact
- Add contact detail page in dashboard UI showing entity document and claims
- Add phone call hangup endpoint via Twilio API
- Add phone call status persistence (ringing, active, completed, duration, recording path) to database
- Add schema migrations 307–313: memory tables in SQLite, phone/email origin session links, simplified bulletins, entity-bulletin links, session range tracking, drop session summaries table
- Add SQLite-backed memory storage replacing file-based bulletin/claim/entity persistence
- Add manual bulletin seeding via CLI (`seed_manual`) for ad-hoc memory injection

### Changed
- Simplify bulletin model from structured metadata (entities, scope, session tracking) to plain-text notes with inline `{{contact:ID|Name}}` tags
- Simplify bulletin generator input from verbose structured transcript to compact message list with sender contact IDs and timestamps
- Replace session summary service with bulletin-based idle session processing in heartbeat task
- Replace summary cards with bulletin cards in dashboard home view
- Update memory dashboard route with enhanced entity browsing and claim visualization
- Migrate memory service from file-based YAML storage to SQLite with related entity parsing from body text
- Gate tap dispatch behind `tap_enabled()` check instead of unconditionally running on every non-tool response
- Update WhatsApp bridge to subscribe to contact presence for patience system integration
- Strengthen bulletin generator prompt to forbid inventing IDs for known contacts
- Pass known entities from ContactDirectory to bulletin generator for canonical ID enforcement
- Add email polling integration with patience gate for coordinated message processing
- Update memory CLI with new seed commands and cleanup orchestration

### Fixed
- Fix phone call recording finalization on call completion with proper path persistence
- Fix phone call status transitions to persist all intermediate states (ringing, in-progress) to database

## 2026-05-31

### Added
- Add entity-centric memory system (v3) replacing wiki/category model: channel-based architecture with bulletin generation from session transcripts, entity resolution, claim extraction, and graph-based entity relationships
- Add memory bulletin generator that converts session transcript ranges into structured draft bulletins with channel, visibility, scope, and entity metadata
- Add memory claim service for extracting knowledge claims from bulletins and linking them to entities
- Add memory entity resolver for mapping contact references, channels, and topics to entity IDs
- Add memory index service for building searchable text indexes from entity directories
- Add memory CLI commands: `seed` (regenerate from session history), `rebuild` (rebuild indexes from bulletins), `validate` (check structure), `query` (natural language search)
- Add frontend error reporting: unhandled exceptions and promise rejections are POSTed to a backend endpoint for centralized logging
- Add live tool call tracking in call detail view: WebSocket events show running tools with pulse indicator and output display
- Add `log_id` to `llm.call.running` and `llm.call.tool_completed` WebSocket events for precise call tracking

### Changed
- Replace wiki/category memory tools with entity-centric tools: `memory_search`, `memory_read`, `memory_browse`, `memory_write`, `memory_graph` with entity types (contacts, groups, channels, trips, locations, events, tasks, artifacts, decisions)
- Replace memory_prompts and people_updates in session summaries with direct bulletin generation from transcripts via the heartbeat task
- Simplify session summary LLM prompt to focus on summary and topics only, removing memory extraction responsibilities
- Switch dashboard session call tool count from counting offered tools (`tools_json`) to counting actual executed tool calls (`function_call` entries in `messages_json`)
- Fix active call tracking in session view to remove only the specific completed call by `log_id` instead of clearing all running calls
- Deduplicate live running calls in session timeline so DB-recorded running calls don't appear alongside WebSocket-tracked calls
- Update memory dashboard dream log display to show per-bulletin breakdown with claims and entity ops instead of category/slug pairs
- Simplify memory prompt in workspace assembler to reference new entity-based tools and omit inline wiki documentation

### Fixed
- Fix animated GIF sending via WhatsApp: preserve GIF animation by passing files under the bridge payload limit as-is, and add frame-dropping resize for oversized animated GIFs instead of flattening to static JPEG

## 2026-05-30

### Added
- Add multimodal image support: Go bridge downloads incoming WhatsApp images to disk and forwards metadata to the Python server, which stores image references in message metadata and reconstructs OpenAI `input_image` content parts in the prompt for GPT-5.5 vision
- Add WhatsApp group understanding with member tracking: new `whatsappgroups` and `whatsappgroup_members` tables replace flat text column, Go bridge handles GroupInfo and JoinedGroup events, member changes trigger LLM dispatch in group sessions
- Add group participants tool for LLM function calling in WhatsApp group sessions
- Add SyncGroups on connect to populate group tables from existing WhatsApp memberships (not just new joins)

### Changed
- Add media_dir configuration to WhatsAppBridgeSettings for configurable image storage path
- Improve workspace read_file tool to recognize image extensions and return a descriptive message instead of binary file error

### Fixed
- Fix migration 305: disable foreign key checks during contacts table recreation to prevent constraint failures from whatsappgroup_members references
- Fix contact detail page: update dashboard API and React component to query new group tables instead of removed whatsapp_groups column
- Fix LLM dispatch and OpenAI service logging to handle multimodal message content (list of content parts) without crashing

## 2026-05-28

### Added
- Add subagent system with Claude Code CLI integration, replacing skill-specific delegation with a generic async subagent service that spawns Claude processes and tracks status, cost, and results
- Add subagent tools (create_subagent, message_subagent, kill_subagent) for LLM function calling, with automatic result injection back into parent WhatsApp sessions
- Add subagent lifecycle management: stale subagent cleanup on startup, status tracking, and event-driven result delivery via event bus
- Add session messages to dashboard session detail view, showing conversation entries from session_messages table alongside LLM calls and summaries in the timeline
- Add subagent session classification in dashboard to distinguish subagent sessions from WhatsApp sessions by key prefix
- Add visual distinction for subagent messages in session timeline: amber for task messages (→ subagent) and teal for response messages (subagent →)
- Add diagnostic logging for empty OpenAI responses to capture refusal details, status, and output types
- Add skill-guru skill to guide creation of new workspace skills via subagent delegation

### Changed
- Replace full memory index (~8KB) in system prompt with concise memory tool reference (~400 bytes), reducing per-call token usage by instructing the agent to use memory_search instead of loading all entries
- Add explicit send_whatsapp_message instruction to incoming WhatsApp user prompts to improve tool call reliability with gpt-5.4-mini
- Update subagent result notification to instruct agent to relay results via send_whatsapp_message instead of only referencing message_subagent/kill_subagent
- Update session agenda template to reference subagent tools instead of deprecated skill delegation
- Replace skill delegation tools with subagent tools in the tool registry

### Fixed
- Fix WhatsApp reply delivery failure caused by empty chat_id on DM session routes: store chat_id (WhatsApp JID) alongside contact_id for DM routes so subagent result dispatch can resolve the outbound address
- Relax session_routes CHECK constraint and Pydantic validator to allow DM routes to include chat_id
- Backfill missing chat_id on all existing WhatsApp DM routes from metadata sender_jid
- Fix subagent sessions displaying as "whatsapp" channel in dashboard by checking subagent: prefix before :whatsapp: in channel parser

## 2026-05-24

### Added
- Add centralized tool registry with `build_common_tools()` replacing duplicated tool assembly across WhatsApp and email dispatchers
- Add tap dispatch system: follow-up LLM call when agent doesn't use send tool, replacing auto-send of raw text output
- Add TapCard UI component in dashboard to visually distinguish tap follow-up dispatches from regular messages
- Add dreaming memory system with bulletin pipeline, LLM-driven dream curation, and conflict resolution across entries
- Add reply tracking to WhatsApp and email dispatch to detect whether agent called the send tool

### Changed
- Rewrite all session agenda templates (WhatsApp, email, phone) with prominent DELIVERY sections instructing the agent to use send tools
- Update grounding rules to emphasize text output is invisible and only tool calls have effect
- Convert memory writes to bulletins: both manual `memory_write` and automatic `reflect_and_update` now produce bulletins for dream curation instead of direct category writes
- Trigger memory dream process after each heartbeat summary batch to curate bulletins into proper categories
- Pass session metadata (time window, participants, contact IDs) through to memory reflection
- Update ARCHREVIEW.md tool registry item to reflect centralized tool assembly

## 2026-05-23

### Added
- Add memory wiki subsystem with search, reflection from session summaries, bulk seeding CLI, dashboard search UI, and LLM function-calling tools
- Add docs search service with LLM-powered documentation querying and function-calling tools

## 2026-05-22

### Removed
- Remove unused services and routers, streamline codebase

## 2026-05-21

### Added
- Add reflection service for on-demand LLM reflection on session history
- Add rich text component for dashboard UI rendering
- Add generate-docs skill for rebuilding documentation from DOCS.yaml

### Changed
- Restore phone call subsystem with updated integration

## 2026-05-17

### Added
- Add workspace browser UI with file listing, content viewing, and file editing
- Add contact editing in the dashboard with editable contact fields

### Changed
- Make workspace layout responsive: stacked panels on mobile, side-by-side on desktop
- Use vertical file list on mobile workspace instead of horizontal scroll
- Improve WebSocket reliability for dashboard live updates

### Fixed
- Fix workspace image viewing to use FileResponse instead of read_bytes

## 2026-05-16

### Added
- Add session summaries with idle-triggered generation, topic extraction, and dashboard display

### Changed
- Link session summaries to participants and contacts in the dashboard

## 2026-05-11

### Added
- Add session participants tracking with contact resolution, participant name maps, and dashboard UI
- Add WhatsApp outreach tools for initiating conversations with contacts from the dashboard
- Add Claude Code skill delegation system with skill loader, developer service, and frontmatter-based skill parsing

### Changed
- Add WhatsApp NO_REPLY support and auto-send fallback for message delivery

### Fixed
- Fix outreach tool to record full turn in target DM session history

## 2026-05-10

### Added
- Add email and WhatsApp tools for LLM function calling
- Add workspace context injection into agent sessions

### Changed
- Consolidate LLM dispatch to use OpenAI as the sole provider, removing Z.ai provider support
- Swap default model to gpt-5.4-mini

## 2026-05-09

### Added
- Add custom LLM harness with unified dispatch service, tool calling framework, OpenAI-compatible provider, and eval framework
- Add WhatsApp bridge companion service: Go/whatsmeow bridge with persistent queue, WebSocket protocol, and Python-side integration

### Changed
- Route WhatsApp messages through the new LLM dispatch service instead of the deprecated OpenClaw agent gateway

### Fixed
- Fix eval judge blind spot where responses were not properly scored, and align voice evals with production prompt format

## 2026-05-06

### Added
- Add barge-in support and call initiation for phone calls with warmup pipeline and silence detection
- Add phone call subsystem with Twilio integration: outbound/inbound calls via media stream, mu-law audio codec, call recording, and call dashboard

### Fixed
- Fix phone call warmup and silence detection for Twilio media stream calls
- Add ringing and canceled statuses to phone call state machine

## 2026-05-03

### Added
- Add dispatch tracking system with database schema, service layer, and API endpoints for monitoring agent dispatch lifecycle
- Add heartbeat framework with registerable background tasks, cron expression parser, and shared AppContext
- Add voice chat subsystem with real-time STT/TTS engines, WebSocket transport, and bundled reference voices

### Changed
- Refactor dashboard router from a single 2285-line module into a package of sub-modules
- Refactor service layer to accept AppContext instead of raw Database, standardizing dependency injection

### Fixed
- Resolve stuck dispatches on task tap completion

## 2026-05-02

### Changed
- Improve dispatch system reliability and add contact tools for LLM contact lookup and management

## 2026-05-01

### Added
- Add contact trust system with trusted/untrusted sender classification and collapsible email message views

## 2026-04-30

### Changed
- Harden email prompt guidance: enforce reply-vs-send distinction, add identity verification warnings for untrusted senders
- Enforce email thread agendas and fix attachment downloads

### Fixed
- Fix project dispatch routing for next_action notifications

## 2026-04-29

### Added
- Add email attachment support with per-attachment download control for untrusted senders

## 2026-04-28

### Added
- Add email relay system via AgentMail: polling, sending, replying, and inbox management

### Changed
- Filter WhatsApp notification delivery to only needs_input and project_result types

### Fixed
- Fix AgentMail integration bugs including session agenda seeding

## 2026-04-25

### Fixed
- Fix notification routing for auto-created project tasks that have no delivery route
- Fix task file validation to check file existence on disk before registering

### Changed
- Include full user response in next-action prompt after block approval

## 2026-04-24

### Added
- Add Ed25519 device identity for gateway websocket authentication

### Fixed
- Fix doctor command crash when project_id or approval_id is missing

## 2026-04-23

### Added
- Add openclaw-skill pip package with SKILL.md for installable skill

### Changed
- Restructure monolithic codebase into three pip packages (cyborg-core, cyborg-cli, cyborg-server) with proper pyproject.toml files
- Remove hardcoded project workspace paths across config, services, and CLI

### Removed
- Remove planning and progress documentation files
- Remove cyborg-context npm package
- Remove openclaw-plugin source code, slimming plugin to a thin wrapper

## 2026-04-21

### Added
- Add source project discovery and linking: auto-discover related closed projects and link them as sources via CLI and API

## 2026-04-15

### Added
- Add project blocking with user approval flow: create task_input approvals when projects are blocked, enabling dashboard resume

### Changed
- Improve project unblocking after user approval with anti-re-blocking instructions in reasoning prompt

## 2026-04-13

### Changed
- Improve reasoning tuning and prevent agents from using project delete

## 2026-04-12

### Added
- Add project pause/resume controls with CLI commands, dashboard buttons, and background reasoning resume
- Add project notification muting with CLI commands and per-project mute field

### Changed
- Remove plan text from task assignment prompts to reduce confusion; include input file information instead
- Make notification dispatch non-blocking: fire-and-forget pattern instead of blocking API responses

### Removed
- Remove plan service and /plans router; plan functionality now handled through project specs and reasoning

## 2026-04-08

### Added
- Add structured task input approvals: text and multi-choice input schemas for task blocking, with dashboard approval forms
- Add async next-action decision flow with CLI command and OTP-secured API endpoint

## 2026-04-07

### Changed
- Enforce one task at a time per project to prevent concurrent execution conflicts

## 2026-04-06

### Changed
- Lock spec approvals to dashboard UI only, remove state from project updates

### Fixed
- Fix notification retry timing

## 2026-04-05

### Added
- Add task file tracking with CLI command and API endpoint for registering files produced during task execution
- Add upstream task context in reasoning: build parent task results and output file context, inject into all reasoning prompts
- Add fresh reasoning sessions using unique session keys per reasoning call to prevent cross-contamination

### Changed
- Flip auto_execute default to true: projects now auto-execute by default
- Simplify project creation workflow: spec v1 auto-created, plan and method optional
- Make spec method field optional, allowing aim-only projects

## 2026-04-03

### Changed
- Clean up task and spec approval flow

## 2026-04-01

### Added
- Add prompt history recording with database schema, service, and API integration

### Changed
- Improve task execution and reasoning reliability

## 2026-03-29

### Changed
- Refactor dashboard overview to show real workflow state instead of system metrics

### Removed
- Remove standalone tasks dashboard page (merged into other views)

## 2026-03-26

### Changed
- Improve OpenClaw reasoning service with robust JSON response parsing and increased timeouts
- Add planning CLI commands and API endpoints

## 2026-03-22

### Added
- Add learning service for extracting insights from project outcomes
- Add health monitor service with periodic project health checks and risk assessment
- Add structured logging system with correlation IDs, specialized log helpers, and execution timing decorators
- Add database-backed log storage with 30-day retention cleanup trigger
- Add cyberpunk-themed web dashboard with overview, projects, approvals, logs, and health pages
- Add dashboard API with chart endpoints for project status, task breakdown, and health distribution
- Add approvals workflow with database schema and pending/review UI
- Add default contact configuration for unrouted notification delivery
- Add plan approval notifications with context and agent delivery support
- Add correlation ID middleware for HTTP request tracing

### Changed
- Replace mock dashboard data with real database queries across overview, logs, approvals, and project pages
- Simplify DatabaseLogHandler by switching from background thread to synchronous SQLite writes
- Refactor structured logging module and clean up handler implementation
- Improve OpenClaw reasoning service with robust JSON response parsing and increased timeouts

### Fixed
- Fix parsing of success_criteria JSON field in project detail dashboard template
- Fix logs page to query actual structured_logs table instead of using hardcoded mock data
- Fix WhatsApp DM session key format to match expected format
- Fix DatabaseLogHandler global variable initialization

### Removed
- Remove old OpenClaw SKILL.md (replaced by native Context Engine plugin)

## 2026-03-21

### Added
- Add comprehensive CLI with full CRUD commands for tasks, projects, contacts, notifications, session routes, webhooks, events, and OpenClaw integration
- Add OpenClaw reasoning service with plan generation, task evaluation, strategy refinement, and health analysis
- Add context builder service for assembling project/task context for OpenClaw prompts
- Add test suites for OpenClaw acceptance testing and project execution

## 2026-03-10

### Added
- Add FastAPI-based Cyborg data service with SQLite backend and comprehensive CLI
- Add project autonomy service with self-executing projects and plan management
- Add project execution service for automated task orchestration
- Add notification delivery system with channel routing, session route registry, and webhook processing
- Add OpenClaw integration with hook-based gateway communication
- Add test suites for API endpoints, CLI commands, project execution, and webhooks
