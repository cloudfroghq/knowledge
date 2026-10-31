---
name: approve-transition
description: Issues the human token that unlocks, for this session, a Jira transition that `guard-jira-transition.sh` blocked because it is not a reversible forward transition.
when_to_use: when `jira_transition_issue` was blocked by `guard-jira-transition.sh` and a person, outside the agent, decided that the transition must go ahead.
allowed-tools: ["Read", "Bash(.claude/skills/approve-transition/write-approval.sh *)"]
disable-model-invocation: true
user-invocable: true
argument-hint: "<ticket-key> <transition-id>"
---

# approve-transition

Human counterpart of `.claude/hooks/guard-jira-transition.sh` (ADR-031). The hook lets reversible forward transitions through without approval (`61` In Analysis, `111` In Develop, `121` In Code Review); for any other transition — intermediate or terminal, according to the ADR-031 classification — it requires the variable `CD_JIRA_APPROVED_TRANSITION` to match the requested id: without a token there is no progress. This skill is the only legitimate way to set that variable backed by a human decision, and that is why `disable-model-invocation: true`: the model never invokes it on its own, only the person does.

## Steps

1. Confirm, outside the agent, that the transition must be applied — a person approves the business decision, not the execution.
2. Run `.claude/skills/approve-transition/write-approval.sh <ticket-key> <transition-id>`. The script rejects the ids `61`, `111` and `121` (they need no approval) and, for the rest, writes a token to `$CD_APPROVALS_DIR/<ticket-key>-<transition-id>.json` (by default `~/.knowledge/approvals/`) with who approved and when, and a TTL of 30 minutes.
3. Export `CD_JIRA_APPROVED_TRANSITION=<transition-id>` in the same session, exactly as the script's output says — it is the condition that `guard-jira-transition.sh` checks to let the call through.
4. Invoke `jira_transition_issue` with that id. The hook compares `CD_JIRA_APPROVED_TRANSITION` against the call's `transition_id` and, if they match, lets it through.
5. `mem_save` recording who approved, when, the ticket and the transition.

## When NOT to use it

- For `61`, `111` or `121`: the hook already allows them without this step.
- To draft or publish a reply to Triara or to the customer: a person does that directly; this skill only unlocks the Jira transition (ADR-031).
