import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'

const [output, ...files] = process.argv.slice(2)
if (!output || !files.length) throw new Error('Provide the output JSON path and results.json files')
function cleanupMetrics(value) {
  if (!value) return undefined
  const { success, cleanup_complete, cleanup_success, closed_browser, unbound_windows,
    closed_explorers, baseline_window_count, windows_before_cleanup, windows_after_cleanup, browser } = value
  return { success, cleanup_complete, cleanup_success, closed_browser, unbound_windows,
    closed_explorers, baseline_window_count, windows_before_cleanup, windows_after_cleanup,
    browser: browser && { status: browser.status, closed_pages: browser.closed_pages },
    stopped_operations: value.stopped?.operations?.length,
    closed_targets: value.closed?.length,
    failed_target_closes: value.closed?.filter(target => target.success !== true).length,
    error: value.error ? 'Cleanup failed. Inspect the local raw report.' : undefined }
}
const trials = []
for (const file of files) {
  const report = JSON.parse(await readFile(file, 'utf8'))
  for (const trial of report.trials) {
    const { tool_values, answer, session_evidence, cleanup, peer_cleanup, restored_cleanup, ...metrics } = trial
    const headers = session_evidence?.headers || []
    trials.push({ ...metrics, cleanup: cleanupMetrics(cleanup), peer_cleanup: cleanupMetrics(peer_cleanup),
      restored_cleanup: cleanupMetrics(restored_cleanup), source: file, session_evidence: session_evidence && {
      rows: session_evidence.rows, sessions: session_evidence.sessions,
      headers, unique_observations: session_evidence.unique_observations,
      backend_totals: session_evidence.backend_totals, text_bytes: session_evidence.text_bytes,
      attachments: session_evidence.attachments,
      error: session_evidence.error,
    } })
  }
}
const groups = Map.groupBy(trials, trial => `${trial.version}|${trial.model}|${trial.task}`)
const quantile = (values, proportion) => values.toSorted((a, b) => a - b)[Math.ceil(values.length * proportion) - 1] ?? null
const summary = [...groups.values()].map(rows => {
  const completed = rows.filter(row => row.status === 'PASS')
  const times = rows.flatMap(row => row.samples.map(sample => sample.ms))
  const backendKeys = new Set(rows.flatMap(row => Object.keys(row.session_evidence?.backend_totals || {})))
  const backend = Object.fromEntries([...backendKeys].map(key => [key,
    +rows.reduce((sum, row) => sum + (row.session_evidence?.backend_totals?.[key] || 0), 0).toFixed(3)]))
  const usage = Object.fromEntries(['inputTokens', 'outputTokens', 'totalTokens', 'cacheReadTokens', 'cacheWriteTokens']
    .map(key => [key, rows.every(row => typeof row.usage?.[key] === 'number')
      ? rows.reduce((sum, row) => sum + row.usage[key], 0) : null]))
  const usageObserved = Object.fromEntries(Object.keys(usage).map(key => [key,
    rows.every(row => typeof row.usage_observed?.[key] === 'number')
      ? rows.reduce((sum, row) => sum + row.usage_observed[key], 0) : null]))
  const providerImages = rows.flatMap(row => row.provider_requests?.flatMap(request => request.images) || [])
  return { version: rows[0].version, model: rows[0].model, task: rows[0].task,
    attempts: rows.length, passed: completed.length, failed: rows.length - completed.length,
    total_e2e_ms: +rows.reduce((sum, row) => sum + row.end_to_end_ms, 0).toFixed(3),
    failed_e2e_ms: +rows.filter(row => row.status !== 'PASS').reduce((sum, row) => sum + row.end_to_end_ms, 0).toFixed(3),
    successful_e2e_p50_ms: quantile(completed.map(row => row.end_to_end_ms), .5),
    successful_e2e_p95_ms: quantile(completed.map(row => row.end_to_end_ms), .95),
    tool_samples: times.length, tool_p50_ms: quantile(times, .5), tool_p95_ms: quantile(times, .95),
    total_model_requests: rows.reduce((sum, row) => sum + row.model_requests, 0),
    total_tools: rows.reduce((sum, row) => sum + row.tools, 0),
    total_provider_images: rows.reduce((sum, row) => sum + (row.provider_image_count || 0), 0),
    provider_image_bytes: providerImages.every(image => typeof image.bytes === 'number')
      ? providerImages.reduce((sum, image) => sum + image.bytes, 0) : null,
    provider_image_pixels: rows.reduce((sum, row) => sum + (row.provider_image_pixels || 0), 0),
    usage, usage_observed: usageObserved, usage_complete_trials: rows.filter(row => row.usage_complete).length,
    backend_totals: backendKeys.size ? backend : null,
    exact_cost: null, separate_image_cost: null }
})
await mkdir(dirname(resolve(output)), { recursive: true })
await writeFile(output, JSON.stringify({ summary, trials }, null, 2) + '\n')
console.log(JSON.stringify(summary, null, 2))
