export const meta = {
  name: 'review-changes',
  description: 'Reviews the diff for coroutine safety, the tool contract and the vault boundary; verifies each finding with a second agent',
  phases: [{ title: 'Review' }, { title: 'Verify' }],
}

const DIMENSIONS = [
  {
    key: 'async-safety',
    prompt:
      'Review the knowledge diff looking for async functions called without await (the class of bug that broke compare_flows and search_deep with "coroutine is not iterable"), or a new tool without an async test or a schema test. Report findings with file, line and a concrete failure scenario.',
  },
  {
    key: 'tool-contract',
    prompt:
      'Check whether the diff changes the response shape of any tool (including the shape of file_path: prefix or wikilink brackets) without a tool version bump or an update of the server\'s tool inventory in the vault.',
  },
  {
    key: 'vault-boundary',
    prompt:
      'Check whether the diff introduces a server write inside a vault outside 90 - Memory/, or a test that points at the real vault instead of tests/fixtures/vault/.',
  },
]

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'number' },
          summary: { type: 'string' },
          failure_scenario: { type: 'string' },
        },
        required: ['file', 'summary', 'failure_scenario'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    isReal: { type: 'boolean' },
    reasoning: { type: 'string' },
  },
  required: ['isReal', 'reasoning'],
}

const results = await pipeline(
  DIMENSIONS,
  (d) => agent(d.prompt, { label: `review:${d.key}`, phase: 'Review', schema: FINDINGS_SCHEMA }),
  (review) =>
    parallel(
      review.findings.map((f) => () =>
        agent(
          `Adversarially verify this finding against the real diff: ${f.summary} (${f.file}:${f.line}). ${f.failure_scenario}`,
          { label: `verify:${f.file}`, phase: 'Verify', schema: VERDICT_SCHEMA }
        ).then((v) => ({ ...f, verdict: v }))
      )
    )
)

const confirmed = results.flat().filter(Boolean).filter((f) => f.verdict?.isReal)

return { confirmed }
