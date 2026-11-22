# AI interaction log — Booking audit, report and ADR preparation (19–22 Nov)

Reconstructed from the available conversation, code, Git history and recorded
test results. Prompts are summaries, not quotations.


## 1. Understanding the material and scoping Booking
 
| Field | Details |
| --- | --- |
| Tool | Claude (claude.ai chat). |
| Prompt | I asked Claude to read the lectures, labs, brief and team plan and explain what Booking had to do, without code. |
| Output | An overview of Booking's responsibilities and a mapping of the labs (messaging, sagas, circuit breaker) onto them. |
| Issues | Claude took some team-plan numbers as fact, e.g. that Payment retries for about 12 s. The real Payment code does not retry. |
| Decision | Modified |
| Fix | I made the code and the contract the source of truth, not the plan. |
| Learned | A plan describes intentions; the code describes behaviour. |


## 2. Booking contract and the team's open questions
 
| Field | Details |
| --- | --- |
| Tool | Claude (claude.ai chat), reading the repo. |
| Prompt | Draft the Booking contract and a team message with defaults; then review the draft against the team's real code. |
| Output | The contract and a team message with a default for each open question. |
| Issues | The review caught Claude's own error: a Payment timing taken from the team plan that the code did not match, plus a few incoherences. |
| Decision | Modified |
| Fix | Corrected before committing; the checkout design stayed, for the right reason. |
| Learned | The plan is not ground truth where the code disagrees, and the AI's claims need checking too. |


## 3. Checkout, the Payment call and the circuit breaker
 
| Field | Details |
| --- | --- |
| Tool | Claude Code in plan mode, then normal mode. |
| Prompt | For each complex step: a plan-mode prompt first (decisions made, questions to answer from the code), then a build prompt once I had checked the plan. |
| Output | Checkout that answers at once and calls Payment in the background, with retries, deadlines and a circuit breaker. |
| Issues | The plan had a breaker flaw that could strand bookings. Claude Code misreported the files' line endings and tried to edit the contract during a build. |
| Decision | Modified |
| Fix | Breaker corrected in the plan; files checked directly; contract changes made a separate, verified step. |
| Learned | Planning first removes ambiguity. A tool's report about the files is not proof; the files are. |


## 4. Load test and evidence
 
| Field | Details |
| --- | --- |
| Tool | Claude Code. |
| Prompt | Test 500 buyers on the last seat, add dashboard panels and check a fresh start, without changing Booking's logic. |
| Output | A repeatable load test with saved reports: one winner every run, no errors. |
| Issues | The test harness itself lost a request, and an old test left messages blocking the run. Claude made two mistakes of its own: a wrong assumption about metrics and an over-broad cleanup command. |
| Decision | Modified |
| Fix | The harness was fixed, not Booking; failed runs were kept as evidence; the slow replies were recorded as a known limit. |
| Learned | A test harness can be wrong too. Under a sell-out, what breaks first is waiting time, not correctness. |


## 5. Consistency audit of Booking

| Field | Details |
| --- | --- |
| Tool | Claude Code (ran it in plan mode) |
| Prompt | Before writing the report, check that Booking's code matches its contract, and that other services and the docs agree with it. |
| Output | Booking matched its contract, and every message and flow across services worked. The audit also found out-of-date docs and a compose setting that switched seat holds off in the UI. |
| Issues | Claude got one fact wrong (it called a thread limit a connection pool) and suggested small fixes and extra tests. |
| Decision | Modified |
| Fix | I skipped the test run (I had just run the suite) and decided nothing found was critical, so no code changed. The wording was corrected, and the new doc issues went to the teammates who own them. |
| Learned | The audit's job was to prove my contract holds, not to polish. |
