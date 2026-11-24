import { execFileSync, spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { openSync, closeSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { createKernel } from '../kernel.js'

const home = join(process.env.LOCALAPPDATA, 'dsh-p7-legacy')
const source = resolve('.runtime/p7-legacy/kernel/dsh_desktop')
const installed = JSON.parse(await readFile('.runtime/p7-legacy/package.json', 'utf8'))
if (installed.version !== '0.4.14') throw new Error('Expected the archived 0.4.14 baseline')
async function probe(token) {
  const response = await fetch('http://127.0.0.1:8768/api/dsh/health', {
    headers: { Authorization: `Bearer ${token}`, 'X-DSH-Owner': '0'.repeat(64) }, signal: AbortSignal.timeout(1000),
  })
  const health = await response.json()
  if (!response.ok || health.protocol !== 1 || resolve(health.bridge?.source_root || '') !== source) {
    throw new Error('Legacy source identity mismatch')
  }
  return health
}
if (process.argv.includes('--check')) {
  const state = JSON.parse(await readFile(join(home, 'kernel-state.json'), 'utf8'))
  const health = await probe(state.token)
  if (health.bridge.pid !== state.pid) throw new Error('Legacy worker PID mismatch')
  console.log(JSON.stringify({ legacy_pid: state.pid, protocol: health.protocol, source: health.bridge.source_root }))
  process.exit(0)
}
const restore = process.argv.includes('--restore')
if (restore) {
  const state = JSON.parse(await readFile(join(home, 'kernel-state.json'), 'utf8'))
  const health = await probe(state.token)
  if (health.bridge.pid !== state.pid) throw new Error('Legacy worker PID mismatch')
  const response = await fetch('http://127.0.0.1:8768/api/dsh/web', {
    method: 'POST', headers: { Authorization: `Bearer ${state.token}`, 'X-DSH-Owner': '0'.repeat(64),
      'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'close' }), signal: AbortSignal.timeout(30000),
  })
  const closed = await response.json()
  if (!response.ok || closed.success === false) throw new Error('Legacy browser close failed')
  execFileSync('taskkill', ['/PID', String(state.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
}
const current = createKernel({ port: 8766, tokenEnv: 'DSH_DESKTOP_BRIDGE_TOKEN', manageKernel: true,
  autoStart: true, bootSeconds: 120, keepAlive: true })
delete process.env.DSH_DESKTOP_BRIDGE_TOKEN
await current.ensure()
if (restore) {
  console.log(JSON.stringify({ restored_current: current.describe() }))
  process.exit(0)
}
console.log(JSON.stringify({ stopped_current: await current.stop({ force: true }) }))
await mkdir(home, { recursive: true })
await writeFile(join(home, 'config.yaml'), `workspace:\n  weights_dir: ${join(process.env.LOCALAPPDATA, 'dsh-desktop/weights').replaceAll('\\', '/')}\ncron:\n  enabled: false\n`)
const token = randomBytes(48).toString('base64url')
const logs = ['out', 'err'].map(kind => openSync(join(home, `kernel.${kind}.log`), 'a'))
const child = spawn(join(process.env.LOCALAPPDATA, 'dsh-desktop/venv/Scripts/python.exe'),
  ['-m', 'dsh_desktop.dsh_bridge_cli', '--home-dir', home, '--port', '8768'], {
    windowsHide: true, detached: true, cwd: resolve('.runtime/p7-legacy'),
    env: { ...process.env, DSH_DESKTOP_BRIDGE_TOKEN: token,
      DSH_DESKTOP_HOME: home, HERMES_HOME: home, DSH_DESKTOP_CDP_PORT: '9334',
      PYTHONPATH: resolve('.runtime/p7-legacy/kernel'), PYTHONIOENCODING: 'utf-8',
      TEMP: resolve('.runtime/test-temp'), TMP: resolve('.runtime/test-temp') },
    stdio: ['ignore', ...logs],
  })
logs.forEach(closeSync)
child.unref()
await writeFile(join(home, 'kernel-state.json'), JSON.stringify({ pid: child.pid, port: 8768, token }))
for (let attempt = 0; attempt < 120; attempt += 1) {
  try {
    const health = await probe(token)
    // Windows venv launchers can start the real interpreter as a child. The
    // bridge's worker PID must belong to that launch before it enters state.
    const worker = Number(health.bridge.pid)
    const parent = Number(execFileSync('powershell.exe', ['-NoProfile', '-Command',
      `(Get-CimInstance Win32_Process -Filter 'ProcessId=${worker}').ParentProcessId`],
    { windowsHide: true, encoding: 'utf8' }).trim())
    if (worker !== child.pid && parent !== child.pid) throw new Error('Unexpected legacy worker parent')
    await writeFile(join(home, 'kernel-state.json'), JSON.stringify({ pid: worker, port: 8768, token }))
    console.log(JSON.stringify({ legacy_pid: worker, launcher_pid: child.pid, protocol: health.protocol, source: health.bridge.source_root }))
    process.exit(0)
  } catch {}
  await new Promise(resolveWait => setTimeout(resolveWait, 1000))
}
throw new Error('Legacy kernel startup failed. Inspect its isolated home logs.')
