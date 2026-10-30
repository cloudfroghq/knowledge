export const meta = {
  name: 'review-changes',
  description: 'Revisa el diff en seguridad de corrutinas, contrato de tools y frontera del vault; verifica cada hallazgo con un segundo agente',
  phases: [{ title: 'Review' }, { title: 'Verify' }],
}

const DIMENSIONS = [
  {
    key: 'async-safety',
    prompt:
      'Revisa el diff de cd-knowledge-mcp buscando funciones async invocadas sin await (la clase de bug que rompió compare_flows y search_deep con "coroutine is not iterable"), o una tool nueva sin test asíncrono ni test de esquema. Reporta hallazgos con archivo, línea y escenario de falla concreto.',
  },
  {
    key: 'tool-contract',
    prompt:
      'Revisa si el diff cambia la forma de respuesta de alguna tool (incluida la forma de file_path: prefijo o corchetes de wikilink) sin bump de versión de la tool ni actualización del inventario de tools del servidor en el vault.',
  },
  {
    key: 'vault-boundary',
    prompt:
      'Revisa si el diff introduce una escritura del servidor hacia vault/ fuera de 90 - Memory/, o un test que apunte al vault real en vez de a tests/fixtures/vault/.',
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
          `Verifica adversarialmente este hallazgo contra el diff real: ${f.summary} (${f.file}:${f.line}). ${f.failure_scenario}`,
          { label: `verify:${f.file}`, phase: 'Verify', schema: VERDICT_SCHEMA }
        ).then((v) => ({ ...f, verdict: v }))
      )
    )
)

const confirmed = results.flat().filter(Boolean).filter((f) => f.verdict?.isReal)

return { confirmed }
