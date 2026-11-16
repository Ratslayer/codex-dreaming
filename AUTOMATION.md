# Hourly aggregation procedure

Interpreter: `python3`.
CLI: `kb.py` in this workspace. No external model API is called by the CLI;
the scheduled Codex run performs inference.

## Dispatch

The procedure below is for a timer-triggered turn. For a human follow-up in this
task, handle that request through agent-kb-review instead of starting a fresh
aggregation batch. A confirmed v2 Publish selected submission authorizes publishing
its checked rules/merges and displayed edits without another approval. Source-view
requests and saved interaction state remain read-only. Scheduled turns cannot
authorize publication themselves.

1. Discover the Codex app tools via ALL_TOOLS. Call list_projects. Collect local
   projects and Codex thread metadata with list_threads in history mode, host
   local, both active and archived inventories, paginating to a 30-day updatedAt
   cutoff. Persist ONLY id/projectId/cwd/updatedAt for threads, never summaries.
   On initial import or missing metadata, paginate older history as needed to
   locate specific digest session IDs; do not silently assign a project by title.
   Save `.aggregator/inventory.json` containing `projects`, `threads`, and coverage.
   Refreshing this inventory does not enroll this KB workspace as a source project.
2. Run `kb.py begin --inventory .aggregator/inventory.json`. If busy, stop; do not
   steal a live lease. Keep the returned run ID. Use the CLI for all writes to
   managed files. Intermediate analysis JSON can be written under the run folder.
3. For each stage, in order `session`, `project`, `global`, run
   `kb.py prepare --run-id RUN --stage STAGE`. It returns bounded job file paths.
   Read each job, generate the result described below, save it as a JSON file
   under the run folder, then call
   `kb.py record --run-id RUN --job-id JOB --result RESULT_FILE`.
   Prepare the next stage only after recording every job in the current stage.
4. If a session digest is insufficient for a material inference, call read_thread
   for that exact thread ID, paging relevant turns. Do not retain reasoning items.
   Preserve only necessary messages or tool excerpts with an input file:
   `{"thread_id":"SESSION", "items":[{"type":"userMessage", "id":"ITEM",
   "turn_id":"TURN", "text":"exact relevant text", "truncated":false}]}`.
   Allowed types: userMessage, agentMessage, commandExecution, mcpToolCall.
   Record it using `kb.py evidence --session-id SESSION --input FILE`.
   Cite the returned source_hash with an exact quote. A missing/truncated record
   cannot establish totals, absence of an event, or successful completion.
5. Run `kb.py finish --run-id RUN`. If unable to finish a job, report the error
   and use `finish --run-id RUN --abandon` so unprocessed work remains pending.
   Do not improvise repairs, alter source notes, or publish learned guidance.
6. After closing the lease, present the current actionable pending changes using
   agent-kb-review and its `references/presentation.md`. Render a live checkbox
   list with independent foldouts and a Publish selected button directly in this
   task's final response. Include outstanding unchecked candidates as well as new
   changes, omit unchanged active rules, deduplicate repeated entries, and show
   each merge as one explicit replacement choice. Reuse unchanged reviewed data
   when proposal/evidence hashes still match; do not rerun session inference just
   to redraw the list. Finish with a short no-change message if nothing actionable
   remains. Do not substitute a long report or links-only inbox for the checklist.

The CLI checks the five-minute source quiet period, caps session work at ten,
deduplicates unchanged inputs, and schedules global consolidation after 09:00
Europe/Moscow once daily. Changes to assessments supporting global guidance can
trigger an earlier global review. Hourly runs catch up after downtime.

## Session result

```json
{
  "lessons": [{
    "statement": "Specific behavior supported by the evidence.",
    "condition": "When this behavior applies.",
    "evidence": [{"session_id": "SESSION", "source_hash": "JOB_INPUT_HASH", "quote": "Exact source passage"}]
  }],
  "metrics": {},
  "assessments": []
}
```

Return an existing lesson's `id` when revising it. Do not rewrite an unchanged
lesson merely to cite a newer digest snapshot. Previously extracted lessons are
retained unless explicitly assessed; omission does not delete or invalidate them.
Be selective: routine updates and vague instructions such as "be careful" are not
lessons. Explicit user corrections are evidence; assistant proposals are proposals.

Metric keys: outcome, user_corrections, user_interventions, review_escapes, rework,
lesson_application. Omitted/null means unknown. A measured item is
`{"value":1,"kind":"observed","evidence":[...]}` or kind `inferred`.
Counts apply only to the observed material. Use outcome strings such as
completed/partial/blocked/abandoned/unknown, and separate agent claims from
verified outcomes. Digests are selectively written, not a representative census.

Assessments use `lesson_id`, `decision` (disputed/invalidated/superseded/confirmed),
`reason`, and exact `evidence`. They can target an earlier session's lesson and
are always pending. For supersession, also identify the superseded assessment or
lesson in the rationale; do not erase history.

## Project/global result

If no justified change exists, return:
`{"no_change":true,"reason":"Evidence-based reason."}`.

Otherwise return the COMPLETE proposed set of entries, preserving existing IDs:

```json
{
  "entries": [{"text":"Concise, scoped guidance.", "lesson_ids":["lesson-EXISTING"]}],
  "reason":"Why this exact change is justified."
}
```

Global results also require `generalization`: explain why the guidance applies
beyond the originating projects. Global entries can cite only lessons already
represented in approved project knowledge. A single project's repetition is not
proof of global applicability. Existing global guidance may remain pending review
even if its project-level support is withdrawn; propose the necessary correction.

For a withdrawn entry, omit it from the proposed set and explain the evidence.
Consolidate genuine near-duplicates when they have the same actionable meaning and
compatible conditions. Retain every exception and the union of lesson evidence.
Prefer an explicit `kb.py merge` suggestion through the review workflow; merged
entries carry exact `replaces` snapshots and `merge_conditions`. Do not combine
different scopes or hide disagreements by merging. Keep existing merge lineage
when carrying entries forward. Merges are drafts until selected for publication.
Review prior rejected proposals; do not paraphrase them to bypass rejection.
Do not include a new rule solely because it appeared in an old summary.
Keep each KB at or below 20,000 characters. Every change remains a proposal.

## Failure and run reporting

The timer turn reports the run result and backlog briefly, followed by the live
review checklist. Include source/access errors or priority contradictions when
present; unsupported withdrawals remain explicit review issues rather than hidden
removals through a positive checkbox. Never claim that an unsubmitted rule became
active. Timer-triggered aggregation does not approve, reject, or roll back rules.
Notification settings belong to the automation configuration, not this procedure.
