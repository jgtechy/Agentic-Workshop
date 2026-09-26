# Deferred work

- source_spec: `_bmad-output/specs/spec-epic-2/stories/2-human-gated-escalation.md`
  summary: Add a test for one model turn with two `escalate_to_human` calls, so each gets its own approver answer applied to the matching call.
  evidence: Every escalation test scripts one call per model message, so a regression in `_decide`'s per-action decisions would pass; parallel escalation calls are unlikely under the policy.
