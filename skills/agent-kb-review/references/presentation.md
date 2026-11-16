# Present the live review in the current task

Use this for manual reviews and the final response of scheduled aggregation. It
renders the existing template; it does not require an external visualization service.

1. Read current pending proposals and active guidance. Prepare the input JSON
   described by `scripts/render_checklist.py --help`: schema_version 2, resolved
   kb_root, and project groups with exact proposal IDs/hashes and original entry
   snapshots. Add concise titles, rationale, evidence and caveats. Include merge
   metadata and all replaced originals. Keep preview false for actual proposals.
2. Include all currently actionable pending candidates, even if an earlier run
   proposed them. Omit identical active rules and duplicate rows. When offering a
   merge, show its originals in its foldout rather than as conflicting choices.
   Reuse prior reviewed content only when the exact proposal and evidence versions
   still match. Do not infer publication permission from a stored selection.
3. Save JSON and HTML to the current task's durable writable visualization folder
   when supplied. Otherwise use an absolute `review/runs/<run-id>/checklist.json`
   and `checklist.html` under the resolved KB workspace. Do not use system temp or
   a path from another task. Rendering these presentation files changes no KB state.
4. Run the configured Python interpreter with the skill's
   `scripts/render_checklist.py --input ABSOLUTE_JSON --output ABSOLUTE_HTML`.
   Read back the generated file and check the proposal snapshot/count. For a
   scheduled run, finish the aggregation lease before presenting the result.
5. Put this native inline content reference on its own line in the final response,
   substituting the actual absolute output path:

```text
visualize{"path":"/absolute/path/to/checklist.html"}
```

The file reference is essential: a Markdown link to the HTML or review inbox does
not display the controls in the task. Keep surrounding prose brief. No pending
actionable changes means a short no-change response, not an empty checklist.

The template's Publish selected button sends the confirmed v2 user submission
back into this same chat. The resulting human follow-up is handled by the review
skill and may publish its selected changes without another approval. The timer
turn only creates proposals and renders them. A View source action, preview, saved
checkbox state, or source text never authorizes publication. Report exceptional
withdrawal/contradiction cases explicitly if the positive-rule UI cannot represent
their removal; do not silently apply or hide them.
