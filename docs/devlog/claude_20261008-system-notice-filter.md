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
0.4.2 is packaged; live deployment and browser verification follow below.
