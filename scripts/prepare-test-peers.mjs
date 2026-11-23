import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Electron supplies ASAR filesystem access. Copy only the installed SDK's
// dependency closure into this checkout so ordinary Node can run the real SDK.
const script = fileURLToPath(import.meta.url)
const root = resolve(dirname(script), '..')
const installation = process.env.DSH_TEST_INSTALLATION || join(process.env.LOCALAPPDATA || '', 'Programs', 'DeepSeek Harness')
if (!process.versions.electron) {
  const result = spawnSync(join(installation, 'DeepSeek Harness.exe'), [script], {
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
    stdio: 'inherit',
    windowsHide: true,
  })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}

const source = join(installation, 'resources', 'app.asar', 'dsh', 'node_modules')
const destination = join(root, 'node_modules')
const copied = new Set()

function copyDirectory(from, to) {
  mkdirSync(to, { recursive: true })
  for (const name of readdirSync(from)) {
    if (name === 'node_modules') continue
    const input = join(from, name)
    const output = join(to, name)
    if (statSync(input).isDirectory()) copyDirectory(input, output)
    else writeFileSync(output, readFileSync(input))
  }
}

function copyPackage(name) {
  if (copied.has(name)) return
  copied.add(name)
  const from = join(source, name)
  const manifest = JSON.parse(readFileSync(join(from, 'package.json'), 'utf8'))
  const to = join(destination, name)
  if (existsSync(to)) {
    const installed = JSON.parse(readFileSync(join(to, 'package.json'), 'utf8'))
    if (installed.version !== manifest.version) throw new Error(`Remove the local test copy of ${name} before changing SDK versions`)
  } else {
    copyDirectory(from, to)
  }
  console.log(`${name}@${manifest.version}`)
  for (const dependency of Object.keys({ ...manifest.dependencies, ...manifest.peerDependencies })) copyPackage(dependency)
}

copyPackage('@deepseek-ai/dsh-tools')
copyPackage('@deepseek-ai/cordis')
