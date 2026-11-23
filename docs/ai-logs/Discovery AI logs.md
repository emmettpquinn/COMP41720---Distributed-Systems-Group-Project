# AI interaction log — Discovery search, Catalogue integration and ADR-003

Reconstructed from the available conversation, my recollection, code, Git history and recorded test results. Prompts are summaries. The first entry covers coding help I remember using. The other entries draw on my Codex chat.

## 1. Coding the Discovery service (reconstructed)

| **Field** | **Details** |
| --- | --- |
| Tool | OpenAI Codex. |
| Prompt | I asked Codex to build a draft and scaffold the Discovery service, then explain the structure as I walked through it. I asked it to suggest changes for me to review before implementing them. |
| Output | Codex drafted the initial structure for Discovery, walked me through how the pieces fit together, and offered suggestions as I worked through the draft. |
| Issues | The draft still needed review before I could use it. I wanted to understand how the service was put together before making changes. |
| Decision | Modified |
| Fix | I reviewed the scaffold with Codex first. Further suggestions were implemented only after review. |
| Learned | Walking through the scaffold helped me understand where new code belonged and decide which changes to make. |

## 2. Catalogue messages and the Discovery index

| **Field** | **Details** |
| --- | --- |
| Tool | OpenAI Codex |
| Prompt | I asked Codex to go through the Discovery and Catalogue branches before I spoke to the team. I wanted to know whether Catalogue was the only service Discovery depended on and what we needed to agree before combining the branches. |
| Output | Codex traced `event.changed` from Catalogue through RabbitMQ into Discovery’s PostgreSQL/pgvector index. It also picked out a second integration point, where the gateway and UI call `GET /search` and `GET /events/{event_id}/similar`. I had submitted the Discovery service and Catalogue integration support in `eccabe5`. Codex’s role in this chat was to review the branches and draft a message I could use with the team. |
| Issues | Catalogue and Discovery had different validation limits. Discovery accepted titles up to 500 characters, descriptions up to 8,000, and up to 50 tags of 200 characters each. Catalogue accepted longer values, so a message could be published successfully and still fail validation when Discovery read it. |
| Decision | Modified |
| Fix | I asked Codex to pin down what had already passed and what still needed agreement. `TEAM-SETUP.md` records a check with Catalogue at `7b9524c` and simulated embeddings. Creating an event made it searchable, updating it changed the result, and cancelling it removed it. That run used one disposable PostgreSQL database with separate service tables. The payload limits and a run with the intended database setup were still open at the time. |
| Learned | I can check that RabbitMQ delivers the right message and still miss a difference in what the two services accept. The producer and consumer need to agree on the limits as well as the fields. |

## 3. Date filtering, fallback and the Discovery demo

| **Field** | **Details** |
| --- | --- |
| Tool | OpenAI Codex |
| Prompt | I asked Codex to compare my branch with the team plan and tell me what was ready to demo. I wanted to see what the existing test runs covered and what I still needed to show on the shared stack. |
| Output | Codex reviewed the date-filtered search, similar events, keyword fallback and versioned index. My commit `cc508e8` added date filtering and boundary tests. In the search code, the response includes the date filter, the search mode and a fallback reason. The setup notes record 46 passing tests against a separate PostgreSQL test database. |
| Issues | The test runs used different setups. The standalone run used real Ollama with synthetic events. The isolated Catalogue run used actual Catalogue messages with simulated embeddings. At the time of the review, I still had to show the workflow with real Catalogue and real Ollama together. |
| Decision | Accepted |
| Fix | Codex turned that gap into a demo sequence. Create a new event in Catalogue and time how long it takes to show up in search against the 30-second target. Update and cancel it, then slow Ollama down and check that the response shows the keyword label and still applies the same date filter. Bring Ollama back and check that semantic results return. This was the proposed rehearsal in the chat. |
| Learned | The date filter is part of the request in both search modes. In a failure demo I need to check which results came back, which label the user saw and whether semantic search recovered. |

## 4. ADR-003 and the cost of semantic search

| **Field** | **Details** |
| --- | --- |
| Tool | OpenAI Codex |
| Prompt | I gave Codex my draft ADR-003, the team plan and the updated project brief. I asked whether my part was nearly finished and what evidence I still needed for the ADR and report. |
| Output | My draft proposed date-filtered semantic search over a versioned index. Codex checked it against the brief and pointed out that I had set out the extra model call and a fallback, but still needed evidence that the ranking was worth the cost. It also picked out two numbers from my draft that measure different things, the 1.5-second model-call timeout and the 30-second freshness target. |
| Issues | The draft explained what the service would do if Ollama was slow. It still needed examples showing when semantic ranking gives a better result than keyword search. The freshness target also needed a timed run with Catalogue. |
| Decision | Accepted |
| Fix | Codex suggested comparing both modes on a query where semantic ranking helps and on a query where keywords work just as well. It also suggested checking the one-page limit in Word and keeping the ADR marked Proposed while the team considered it. The available chat records feedback on my draft and the suggested checks. |
| Learned | To explain the trade-off, I need a relevance example alongside the timing. How long I wait for an embedding and how long a Catalogue change takes to reach the index are separate measurements. |

## 5. Discovery branch and pull request

| **Field** | **Details** |
| --- | --- |
| Tool | OpenAI Codex |
| Prompt | I asked for help bringing my Discovery branch into the team’s default branch. I then shared GitHub screenshots when I couldn’t find where to open the pull request. |
| Output | Codex checked the comparison between `master` and `feature/discovery-date-filter`. GitHub showed two commits and 49 changed files and reported that the branches could merge cleanly at the time. Codex suggested a PR title and some wording for the test evidence. |
| Issues | I had referred to the default branch as main. The repository used `master`. There was also a separate Catalogue integration branch, so opening the Discovery PR was one part of getting the services together. |
| Decision | Accepted |
| Fix | I opened the comparison with `master` as the base and my Discovery branch as the compare branch. Codex used the screenshots to point me to Create pull request. For the description it suggested saying that create, update and cancel passed in isolation with simulated embeddings and that the real-Ollama combined check and field limits were still open at the time. |
| Learned | The comparison tells me whether Git can combine the branches. The PR description gives the team the test setup and the checks they still need to see. |
