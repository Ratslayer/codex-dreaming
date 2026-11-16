# Agent KB

Local, review-gated learning for Codex. Obsidian displays these ordinary Markdown
files; it is not required for the CLI or scheduled analysis.

## Setup

Requires macOS or Linux, Python 3.11+, and PyYAML. Node.js is required only
for the JavaScript checklist tests. The CLI uses Unix file locking (`fcntl`).

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
mkdir -p /path/to/session-digests
python3 kb.py init --source /path/to/session-digests
python3 kb.py status
```

Replace `/path/to/session-digests` with a separate directory containing your
existing session digests, or an empty directory for a fresh start. Initialization
creates an empty local KB; this repository ships no learned knowledge, session
history, evidence, proposals, or runtime state. Those generated files are ignored
by Git. Each global or project KB has a 20,000-character rendered-content limit.

To install the reader into your local Codex instructions, optionally run
`python3 install_reader.py` from the activated environment. This explicitly edits
`~/.codex/AGENTS.md` and may increase `project_doc_max_bytes` in
`~/.codex/config.toml`; backups of affected instructions and setting metadata stay
in the ignored `.aggregator/setup-backups/` directory. The installed reader uses
the current Python interpreter, so keep that environment available.

Install the bundled `skills/agent-kb-review` directory in your Codex skills
location to use the review checklist workflow. Scheduling is separate: follow
`AUTOMATION.md` when configuring a Codex automation. No schedule is installed by
the CLI or by cloning this repository.

## Layout

```text
knowledge-base.md                   approved global guidance
projects/<project>/project.json     stable app ID and registered checkout paths
projects/<project>/knowledge-base.md
projects/<project>/sessions/<date>--<session-id>/
    session.md                      current protected source snapshot
    lessons.md                      extracted candidates with stable IDs
    assessments.md                  append-only pending/approved evaluations
    metrics.json                    observed/inferred values; null = unknown
review/inbox.md                     pending proposals and priority contradictions
review/unassigned.md                sessions without reliable project metadata
review/proposals/                   exact proposed changes, hashes, and evidence
.aggregator/                        checkpoints, immutable sources, revisions, runs
```

Extracting a lesson does not approve it. Scheduled runs prepare changes.
Publication follows an explicit decision or a confirmed Publish selected submission.
Pending invalidations leave current guidance effective, as requested.

## Review in Codex

Open `review/inbox.md`, inspect a proposal and its source evidence, then tell Codex
to approve or reject its ID. The review skill also presents an expandable checklist:
clicking Publish selected authorizes the checked rules and displayed edits. Codex
prepares any necessary scoped draft and publishes it without a second approval.
Unchecked rules remain pending. A source note, saved checkbox state, legacy
selection-only message, mockup, or automated prompt provides no such authorization.

The review operation is:

```text
python kb.py approve PROPOSAL --hash REVIEWED_HASH --user-reference CHAT:USER_TURN --reason "User accepted this exact change"
python kb.py reject PROPOSAL --hash REVIEWED_HASH --user-reference CHAT:USER_TURN --reason "User's reason"
```

The CLI rejects stale target versions, modified evidence dependencies, missing
decision references, and repeated decisions. Approval is an audited workflow
boundary, not cryptographic proof of a human identity; the scheduled agent is
explicitly forbidden from invoking these operations. Do not hand-edit managed
files. Active-file drift is detected rather than silently overwritten.

For selected entries or wording changes, create a fresh pending draft:

```text
python kb.py revise PROPOSAL --hash REVIEWED_HASH --selection selection.json --user-reference CHAT:USER_TURN --reason "Requested draft changes"
```

The selection file contains `selected_entry_ids` and optional `edits` mapping
selected IDs to their full revised text. Active guidance is retained. A subset
keeps the parent pending for unselected entries; a full edited selection
supersedes its parent. This command never approves or publishes knowledge.
For a confirmed Publish selected submission, the review skill immediately approves
the scoped draft under that same authorization; there is no second approval step.
Historical selection-only submissions and mockups retain their non-publishing meaning.

## Merge suggestions

The reviewer can propose one combined entry for equivalent guidance in a single
scope. Conditions and exceptions must remain compatible. Session lessons are
retained; the merged entry references all their evidence.

```text
python kb.py merge SCOPE --spec merge.json --user-reference CHAT:USER_TURN --reason "Equivalent guidance with compatible conditions"
```

The spec contains `base_hash` of the current active KB, `replace_entries` with at
least two exact id/text/lesson_ids/lesson_versions snapshots, combined `text`, and
`conditions`. Pending candidates additionally need `proposal_id` and `proposal_hash`.
The command creates a draft only. A checked Merge row explicitly authorizes removal
of those originals on Publish selected; publication replaces them atomically and
preserves unrelated guidance. Stale originals, lost evidence, and overlapping
replacements are rejected. Prior KB revisions remain available for rollback.

## Context and recovery

```text
python kb.py context --cwd /path/to/project
python kb.py context --cwd /path/to/project --query "review authorization"
python kb.py inbox
python kb.py status
python kb.py status --recover
python kb.py rollback PROJECT_ID OLD_CONTENT_HASH --user-reference CHAT:USER_TURN --reason "Explicit rollback request"
```

The Codex global instruction block calls the context reader at task start. It
selects the longest registered checkout prefix, includes approved global and
project entries, and excludes pending work. Unknown projects get global context
only. New chats load the instruction block; ongoing chats refresh on their next
task once they have the block. Explicit current instructions always take priority.

Knowledge is published through a durable, idempotent write journal under a
single-writer lock. Readers refuse incomplete transactions; `status --recover`
replays already-authorized writes. Prior active revisions are preserved locally.
These snapshots are version history, not an off-device backup.

## Automation

See `AUTOMATION.md` for the exact agent protocol. One hourly standalone Codex
automation uses gpt-6.1-sol/high, processes up to ten changed sessions, then affected
projects, then the daily global pass after 09:00 Europe/Moscow. New contradictions
can receive an earlier global assessment. A five-minute quiet period avoids
processing a note while it is being updated. A two-hour lease prevents overlapping
agent runs; expired leases leave unfinished items eligible for a later run.

Inputs are the existing personal digests and app-provided project/chat metadata.
Initial history is the last 30 days; older sessions already imported continue to
update. Newly recognized projects are registered from the local app inventory.
Unmatched session IDs remain unassigned. No filesystem-wide discovery occurs.
Original evidence is fetched only for a specific chat and only needed records are
retained; reasoning records are excluded. Credentials and URL queries are masked.

The Mac must be on and the desktop app running. The next actual scheduled run
recovers pending work after downtime; no wall-clock delivery guarantee is implied.
Every scheduled invocation incurs some model overhead, even with no changed inputs.

## Tests

```sh
python3 -m unittest -v test_kb.py test_merge.py test_review_checklist.py
node test_review_checklist.cjs
```
Tests use temporary fixtures and never change real notes, global settings, or KBs.
