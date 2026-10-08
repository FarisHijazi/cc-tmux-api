# System notices incorrectly attributed to the user

The reported Work-B conversation contained a Claude-generated background-task
notice dated October 7. Its watcher command started October 4 to wait for an
acceptance-test log. The remaining background output contains only `[killed]`;
this does not establish the outcome of the underlying acceptance test.

The JSONL entry has `type=user`, but also `origin.kind=task-notification`,
`promptSource=system`, and `queueTranscriptOnly=true`. The conversation parser
used the role alone, so the UI labeled it "You" and Markdown/HTML sanitization
flattened its XML into an unreadable paragraph.

Plan and change: extend the existing filtering in
[`fleet_probe.py`](../../src/cc_tmux_api/fleet_probe.py) to exclude these internal
records from conversation messages. Do not modify original transcripts or infer
authorship from text patterns. Genuine human messages quoting the same XML remain
in the conversation. No terminal input or task restart is part of this fix.

Validation: 78 fleet/probe tests passed, including three metadata variants and
preservation of identical text genuinely sent by a person. Ruff passed. Version
0.4.2 is packaged and deployed on the Mac and all four existing/new VM service
owners. Runtime configuration and session ownership were preserved.

The full backend suite passed all 217 tests. A fresh Chrome context opened the
exact affected Work-B conversation through the Mac aggregator: 24 messages remain
(7 human, 17 assistant), and the internal notice is absent from both API output
and the rendered conversation. No page errors or session inputs occurred.
Evidence: `/private/tmp/hq-system-notice-report.json` and
`/private/tmp/hq-system-notice-fixed.png`. These are private runtime artifacts,
not repository assets. All VM package bytes match the same 0.4.2 wheel.
