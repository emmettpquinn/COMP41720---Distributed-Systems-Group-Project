# AI interaction log — Payment service

**Name:** John Ogundokun
**Component:** Payment

I developed the Payment component with Codex assistance, including generated
code, tests and documentation. These entries are reconstructed from the
conversation, repository and earlier test results. Prompts are summaries rather
than exact quotations. Test results below refer to earlier runs, not new checks
performed while preparing this log.

## 1. Building Payment and checking repeated requests

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex |
| Prompt | I asked for a Payment service that would connect to the other services, with simple names and functions. Once it was running, I asked for duplicate-payment, refund and restart checks. |
| Output | Codex generated the starter API, storage code and tests. I ran the service locally and used the API documentation to create a payment. The implementation became part of `63b8b0e`. |
| Issues | Booking can send the same payment request again if it does not receive an answer. Payment needs to recognise that booking and return the saved result. A request with the same booking ID but a different amount must also be rejected. |
| Decision | Modified |
| Fix | I requested simpler function names and checks for these cases. Codex ran the checks: repeating the request returned the same payment ID, changing the amount returned 409 Conflict, and the saved payment remained after a restart. |
| Learned | The booking ID connects retries to the original payment. A missing response does not mean the payment failed, so a retry needs to look up the saved result before doing more work. |

## 2. Adding refunds, messaging and failure checks

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex |
| Prompt | I asked to extend my component with Docker startup, RabbitMQ refund messages, failure settings, metrics and a dashboard while Booking was still being developed. |
| Output | I used Codex to generate these additions around the group's shared messaging package. My Payment component gained the refund consumer, outbox sender, delay/error controls and monitoring files (`63b8b0e`). |
| Issues | A repeated message could otherwise refund twice. A crash could also happen after saving a payment but before sending its result. One host-based test reached a different broker without the project exchange. |
| Decision | Accepted |
| Fix | I requested checks for duplicate payments/refunds and restart persistence. Codex ran those checks, plus broker-outage and Notification refund-email checks. The implementation saves payment changes and outgoing messages together and remembers processed refund IDs. The later broker check passed inside the Payment container using its configured connection. |
| Learned | A timeout does not mean a charge failed. Messages can arrive more than once, so both payment retries and refund consumers need duplicate protection. |

## 3. Updating Payment documentation for Booking

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex |
| Prompt | I supplied Erwan's integration messages and updated repository ZIPs and asked what needed changing for Payment. I also asked for the README and ADR to use clearer language. |
| Output | Codex compared the updated code and drafted changes to my README, ADR-004, Payment integration guide and refund demo instructions (`e58dd64`). |
| Issues | Earlier documentation became outdated when Booking added checkout and later when the browser flow was connected. It needed to explain Booking's actual handling of retries and late payments. |
| Decision | Modified |
| Fix | I requested updates for Booking's retries, the safety-net refund flow and saved EUR 45.00 price. Codex revised the documents and checked links and command syntax. I asked for another wording pass so I could understand and explain the decisions. Booking's saga and the checkout UI were other members' contributions. |
| Learned | Booking's 202 response starts checkout; it does not confirm payment. If a payment succeeds after the seat has been released, it must be refunded. |

## 4. Sharing the Payment changes through Git

| Field | Details |
| --- | --- |
| Tool | OpenAI Codex |
| Prompt | I asked how to commit and push just my Payment changes and the shared files they needed, using my existing Desktop folder. |
| Output | Codex explained staging, commits, branches and the remote repository, and supplied terminal commands. I checked the staged file list and carried out the recorded commits and pushes. |
| Issues | My folder originally came from a ZIP. I was unsure whether Git would upload the whole folder, and my first push failed because my account did not have repository access. |
| Decision | Accepted |
| Fix | I used the Git guidance to check which files were included. After I obtained access, I pushed `feature/payment-service`; the terminal confirmed that it was tracking the same branch on the shared repository. |
| Learned | A commit records the staged changes. Pushing sends commits to a remote branch, and pushing a feature branch does not automatically put its changes on master. |
