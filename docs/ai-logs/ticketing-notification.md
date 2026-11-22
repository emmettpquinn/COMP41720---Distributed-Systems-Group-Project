# AI interaction log — Ticketing and Notification, shared messaging and ADR-005

Reconstructed from the available conversation, code, Git history and recorded
test results. Prompts are summaries, not quotations. Measurements are from the
runs noted in each entry.

## 1. Scoping my role and building the shared broker setup

| Field | Details |
| --- | --- |
| Tool | Claude Code (Anthropic Claude) in VS Code. |
| Prompt | I gave Claude the proposal and team plan and asked it to outline exactly what my component owned. I then told it to start building my part of the plan. |
| Output | Claude summarised my scope: tickets, emails, broker topology, ADR-005, the runbook and two report sections. It started with the shared broker setup, because every other service depends on it, and generated the shared messaging package (`f706702`): a topology declared from the plan's message table, a message envelope, a consumer with 1 s / 2 s / 4 s retry queues and a DLQ per queue, a replay CLI and a one-shot `topology-init` container. |
| Issues | The queue and routing names had to match the plan's message table exactly, or other members' services would publish into nothing. The retry ladder also had to end somewhere visible, not loop forever. |
| Decision | Accepted |
| Fix | It was built on the repository skeleton I had already set up for the five services (`b394f07`). It was checked on the running stack: with MailHog stopped, messages moved through the retry queues into the DLQ, and the replay tool delivered them once MailHog was back. |
| Learned | Retries can be built from the broker alone, using TTL queues that dead-letter back to the main queue, so no service needs its own retry loop. |

## 2. Ticketing service

| Field | Details |
| --- | --- |
| Tool | Claude Code (Anthropic Claude) in VS Code. |
| Prompt | I asked Claude to implement the Ticketing service against the plan's success criteria: one ticket per booking, void it on cancellation or refund, send emails through MailHog, and never process a message twice or lose it. |
| Output | Claude generated the FastAPI service, database model, handlers, mailer, Prometheus metrics, Dockerfile and 14 tests (`d669411`). Each message is recorded in `processed_messages` in the same transaction as the ticket change, and unique constraints allow one ticket and one email of each kind per booking. |
| Issues | Two real bugs came up during testing. A concurrency test delivered one booking several times at once and got **three emails**, because the first design relied on row locks that SQLite does not enforce. During the MailHog outage demo, the DLQ-depth gauge stayed at 0 while the queue filled, because the client library cached queue declarations. |
| Decision | Accepted |
| Fix | Claude replaced the locks with a conditional claim (`pending → sending`) with a 60 s lease, plus a sweeper for crashed senders, and added a crash-recovery test. It read the queue depth through the underlying channel instead. I accepted the service only after the live checks passed: 100 copies of one `booking.confirmed` gave 1 ticket and 1 email on Postgres, the gauge went 2 → 0 after replay, and a refunded booking's ticket read as void. |
| Learned | Idempotency has two halves: the database change, which a transaction protects, and the email, which it cannot. A design that reads correctly can still fail under concurrency. Only the test exposed it. |

## 3. Runbook, messaging guide, commits and merge

| Field | Details |
| --- | --- |
| Tool | Claude Code (Anthropic Claude) in VS Code. |
| Prompt | This was part of the same build request: documentation for teammates and operators, then local commits for me to review before pushing. |
| Output | Claude drafted `docs/messaging.md`, `docs/runbook.md` and a first ADR-005 draft (`bb17236`), and split the work into three commits. |
| Issues | The commits included an AI co-author line I had not agreed to. |
| Decision | Modified |
| Fix | Before pushing, I had the line removed from all three commits. I then pushed `feature/ticketing-notification` and merged it into `master` through PR #1 on 10 Nov. AI involvement is recorded in this log rather than in the commit history. |
| Learned | Shared code needs a usage guide, or each member works out the retry/DLQ rules separately. I also need to check what a tool writes into commit messages, not just the code. |

## 4. ADR-005: first draft and rewrite after the contract changed

| Field | Details |
| --- | --- |
| Tool | Claude Code (Anthropic Claude) in VS Code. |
| Prompt | I gave Claude my Lab 2 ADR and asked for ADR-005 in the same layout (17 Nov). After the Booking owner changed the message contract, I discarded that draft, asked Claude to pull the latest `master`, and gave it his messages so it could rewrite the ADR against the new contract. I set the status to Accepted and the limit to one page (22 Nov). |
| Output | The first draft quoted fresh measurements: publish-to-email latency of 37 / 63 / 155 ms (min / median / max), and parking a failing message in 23.2 s instead of the 7 s the retry delays suggested, because of DNS timeouts. For the rewrite, Claude ran full purchases through Catalogue, Booking, Payment and Ticketing. In the late-success case, the booking was released at 9.3 s and refunded at 20.5 s. Ticketing left a void tombstone, issued no ticket, and sent one cancellation email and one refund email. |
| Issues | The first draft predated the contract change: Booking no longer sends `refund_required`, and releases unanswered payments as `payments_unavailable`. The rewrite referred to an ADR-002 that does not exist, and first came out at about 1.4 pages. Claude also noted that the `payments_unavailable` email wording in `notification/app/emails.py` and a `refund_required` mention in `docs/messaging.md` still need updating to match the new contract. |
| Decision | Modified |
| Fix | I rejected the first draft and supplied the agreed contract changes. The rewrite was checked against new end-to-end runs, the false ADR-002 reference was removed, and it was cut to one page before I submitted it. The two follow-ups above are still open. |
| Learned | When two services both send events about the same booking, they can arrive in either order. A ticket state that can only move from valid to void handles that without any special case. An ADR's evidence goes stale when another service's contract changes, so it has to be re-measured, not just reworded. |
