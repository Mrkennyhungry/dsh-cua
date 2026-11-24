import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

if (!process.versions.electron) {
  const executable = join(process.env.LOCALAPPDATA, 'Programs/DeepSeek Harness/DeepSeek Harness.exe')
  const result = spawnSync(executable, [fileURLToPath(import.meta.url), ...process.argv.slice(2)], {
    windowsHide: true, stdio: 'inherit', env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
  })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}

// Use the installation's CLI and credential provider, including its renderer.
// The bridge token stays in memory and never enters the trial report.
if (process.env.DSH_BENCHMARK_BRIDGE_STATE) {
  const state = JSON.parse(readFileSync(process.env.DSH_BENCHMARK_BRIDGE_STATE, 'utf8'))
  process.env.DSH_DESKTOP_BRIDGE_TOKEN = state.token
}
const resources = process.env.DSH_BENCHMARK_RESOURCES
  || join(process.env.LOCALAPPDATA, 'Programs/DeepSeek Harness/resources')
const entry = join(resources, 'app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js')
const { runDesktopCli } = await import(pathToFileURL(entry))
await runDesktopCli(join(resources, 'runtime'))
