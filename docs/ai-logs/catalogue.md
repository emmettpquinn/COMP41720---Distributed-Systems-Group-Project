# AI interaction log — Event Catalogue and shared integration

Reconstructed from the available conversation, code, Git history and recorded
test results. Prompts are summaries, not quotations; results are from earlier
runs.

## 1. Repository review and Docker startup

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex |
| Prompt | I asked Codex to inspect the brief, team plan and branches, identify my Catalogue and shared integration work, and build on the existing Docker setup. |
| Output | Codex mapped the service responsibilities, proposed checkpoints and adapted Compose and startup instructions around the existing shared baseline (`be21a88`). |
| Issues | Shared Docker and service code already existed. The work needed clear ownership and a separate branch rather than changes on another member's branch. |
| Decision | Modified |
| Fix | I made my own branch, file-by-file explanations and explicit permission before commits. Codex adapted the existing setup; later merges preserved the original service commits. |
| Learned | A shared Compose file can start the whole project while each service keeps its own Dockerfile and database. |

## 2. Catalogue scaffolding and database setup

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex. |
| Prompt | I asked Codex to help set up the Catalogue service and its own database  |
| Output | Codex generated the initial FastAPI app, configuration, Dockerfile, health/readiness endpoints and test setup (`be21a88`), followed by the schema and migration runner (`1c109a1`). |
| Issues | Getting the process running was only the first step. Catalogue also needed its own persistent schema and safe startup when PostgreSQL was unavailable. |
| Decision | Accepted |
| Fix | Codex added the private database and migration startup steps. Its tests covered readiness failures, migration rollback and repeat runs. |
| Learned | Scaffolding gives me the service structure. Liveness, database readiness and schema setup each need their own checks. |

## 3. Contracts and example payloads

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex. |
| Prompt | I asked Codex to prepare Catalogue's API and message proposals, then make the agreed changes once the other services were ready. |
| Output | Codex drafted the contract document and JSON examples for event creation, updates, cancellation and full availability snapshots (`fab5a7d`). |
| Issues | The proposal still needed agreement from the other members. IDs, field limits and message contents had to match what their services actually accepted. |
| Decision | Modified |
| Fix | After the Booking acceptance message, I asked Codex to apply the integration updates. It recorded the agreement and aligned Catalogue's text limits with Discovery. |
| Learned | Schema version, event version and availability sequence describe different things. Passing a local test does not establish a shared agreement. |

## 4. Planning and working through Catalogue checkpoints

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex. |
| Prompt | I asked Codex to break the remaining Catalogue work into checkpoints so I had clear targets and could keep track of what was done and what was left. |
| Output | Codex suggested checkpoints covering event/venue APIs, version checks, cancellation, messaging, availability, caching and metrics. It also generated code and tests as we worked through the agreed batches (`7b9524c`). |
| Issues | The implementation had to handle atomic writes, repeated or reordered messages, concurrent updates and availability that had not arrived yet. |
| Decision | Accepted |
| Fix | I chose when to move on to the next batch and approved commits separately. Codex helped implement and test the transaction boundaries, message/sequence checks and cache invalidation. |
| Learned | Catalogue's availability is a read projection. Unknown availability is not zero, and Booking still makes the hold decision. |

## 5. Connecting the services and checkout UI

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex. |
| Prompt | I asked Codex to inspect the other branches, prepare checkout before Booking was ready, then connect it to the implemented contract and update startup checks and documentation. |
| Output | Codex generated gateway routes, checkout/status UI, Compose updates and integration checks. It also exposed the price already saved on a booking (`970258f`, `e947994`). |
| Issues | The early simulated responses differed from the real API: Booking required `customer_id` and returned `booking_status`. A 202 was not confirmation, and Ticketing could be delayed or hold a void record. |
| Decision | Modified |
| Fix | Codex adapted the UI and harness to the real contract. It verified purchase, ticket/email, ownership checks, restarts, decline and late-success refunds in isolated stacks. I approved the integration changes for commit. |
| Learned | Checkout starts work asynchronously. The page must read the booking result, and a timeout must not automatically trigger another payment POST. |

## 6. Larger seating demo and confirmed-seat display

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex. |
| Prompt | I asked for larger demo venues with rows and columns. Later, I reported that a purchased seat still looked selected after refreshing. |
| Output | Codex generated the row layout, event generator and regression tests (`3111509`). It verified a 50-seat event and E10 purchase through the services and in desktop Safari. |
| Issues | The purchase flow worked, but the browser retained its earlier held-seat highlight after confirmation. The initial checks had missed that display-state problem. |
| Decision | Modified |
| Fix | After my report, Codex confirmed the seat was booked in the database, separated confirmed seats from held seats and rebuilt the gateway. All 27 UI tests passed, including confirmation and reload with a stale projection (`da3e1cd`). |
| Learned | Browser selection, a temporary hold and a confirmed booking are separate states. Refresh must restore a confirmed seat as taken. |
