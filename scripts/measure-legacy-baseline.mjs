import { spawn, execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { performance } from 'node:perf_hooks'
import { apply, Config } from '../index.js'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
process.chdir(root)
const state = JSON.parse(readFileSync(join(homedir(), '.dsh/desktop/kernel-state.json')))
process.env.DSH_DESKTOP_BRIDGE_TOKEN = state.token
const tools = new Map()
const effects = []
apply({
  tools: { register: tool => tools.set(tool.name, tool) },
  effect: effect => effects.push(effect),
}, Config({ port: state.port, manageKernel: false, commandFeedback: false }))

// The existing live-test fixture gives this measurement an owned window and
// process. A warm kernel is required so startup and observation stay measurable.
const unique = randomUUID().replaceAll('-', '')
const python = join(root, '.venv/Scripts/python.exe')
const child = spawn(python, ['test/test_cleanup_dialog_live.py', '--child', unique], {
  windowsHide: true, stdio: 'ignore',
})
const execution = {
  agent: { session: { id: `p0-baseline-${unique}` } },
  signal: AbortSignal.timeout(90_000), callId: '',
}
const records = []
let sequence = 0
let windowId

async function call(name, args) {
  execution.callId = `p0-${++sequence}`
  const start = performance.now()
  const value = await tools.get(name).execute(args, execution)
  const success = !value.error && value.success !== false
  records.push({ name, ms: Number((performance.now() - start).toFixed(3)),
    bytes: Buffer.byteLength(JSON.stringify(value)), success })
  if (!success) throw new Error(`${name} failed during baseline measurement`)
  return value
}

try {
  let ready = false
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const windows = await tools.get('doraemon_windows').execute({ action: 'list' }, execution)
    if (JSON.stringify(windows).includes(unique)) {
      ready = true
      break
    }
    await new Promise(resolveDelay => setTimeout(resolveDelay, 200))
  }
  if (!ready) throw new Error('Temporary baseline window did not appear')
  const health = await call('doraemon_health', {})
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    await call('doraemon_windows', { action: 'list' })
    const bound = await call('doraemon_windows', { action: 'bind', label: unique })
    windowId = bound.window_id
    if (!windowId) throw new Error('Fixture binding returned no window id')
    const uia = await call('doraemon_uia', { window_id: windowId, max_nodes: 50, max_depth: 4 })
    const shot = await call('doraemon_capture', { window_id: windowId })
    const imagePath = shot.image_path || shot.SoM_image_path
    // Existing capture may encode JPEG despite the legacy tool's PNG wording.
    // Decode the actual media instead of trusting the name or a PNG header.
    const image = JSON.parse(execFileSync(python, ['-c',
      'from PIL import Image; import sys,json,os; im=Image.open(sys.argv[1]); print(json.dumps(dict(width=im.width,height=im.height,bytes=os.path.getsize(sys.argv[1]),format=im.format)))',
      imagePath], { encoding: 'utf8', windowsHide: true }))
    records.push({ attempt, uiaAttempts: uia.uia_attempts, controls: uia.control_count, image })
    await call('doraemon_windows', { action: 'unbind', window_id: windowId })
    windowId = undefined
  }
  const samples = records.filter(record => record.ms !== undefined).map(record => record.ms).sort((a, b) => a - b)
  const result = {
    version: JSON.parse(readFileSync('package.json')).version,
    toolCount: tools.size, protocol: health.protocol, records,
    summary: { n: samples.length, p50: samples[Math.ceil(samples.length * 0.5) - 1],
      p95: samples[Math.ceil(samples.length * 0.95) - 1],
      totalMs: Number(samples.reduce((sum, ms) => sum + ms, 0).toFixed(3)) },
  }
  mkdirSync('.runtime/p0-fixtures', { recursive: true })
  writeFileSync('.runtime/p0-fixtures/legacy-baseline.json', JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
} finally {
  if (windowId) await call('doraemon_windows', { action: 'unbind', window_id: windowId }).catch(() => {})
  child.kill()
  for (const effect of effects) await effect()?.()
}
