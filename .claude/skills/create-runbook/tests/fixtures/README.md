# Fixtures — create-runbook

`exmp-1234.json` is a **synthetic** ticket in the shape that `jira_search` +
`jira_get_issue` return (`Issue productivo`, Colombia, `Done`), used by the timed
rehearsal in `tests/run-timed-rehearsal.sh`.

Every field is invented: the ticket key, summary, dates, the reporter's name,
phone number, email address and device model (`fixture-user@example.invalid`,
`5500000000`, `"Fixture Tester"`, `"Generic Test Device"`) and the symptoms.

`root_cause_one_line` is an invented value for rehearsal purposes only — the
rehearsal never asserts a real diagnosis, only that the pipeline (fixture →
instantiate → validate against an ephemeral vault) runs end to end.

Do not add a fixture whose `description`/`symptoms` fields contain a real
name, email, phone number, account id or device identifier. `validate-runbook.sh`
check 9 catches emails and 10-digit phone numbers in a runbook's *body*, but a
fixture JSON is consumed before that check ever runs — sanitize by hand first.
