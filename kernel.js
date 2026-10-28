/**
 * Lifecycle for the local Desktop execution kernel (dsh_desktop.dsh_bridge_cli).
 *
 * The plugin starts the Python kernel itself: detached, with a hidden window and
 * stdout/stderr redirected to log files, so a tool call never opens a console
 * window. The bridge token and the kernel PID are cached in a state file so a
 * restarted DSH process reconnects to a kernel that is still running instead of
 * spawning a second one.
 */
import { spawn, spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { delimiter, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HEALTH_OWNER = '0'.repeat(64)
const PROBE_TIMEOUT_MS = 3000
const POLL_INTERVAL_MS = 500
const DEFAULT_KEY_FILE = 'deepseek-api-key'
// /health.protocol: the plugin refuses to drive a kernel that speaks an older
// contract, because the missing endpoints would surface as bare 404s.
export const REQUIRED_PROTOCOL = 1

function packageRoot() {
  // The plugin ships flat: kernel.js sits at the package root, so the package
  // root is this file's own directory. The old form climbed two levels, which
  // only held for monorepo checkouts that nested the plugin under packages/.
  return dirname(fileURLToPath(import.meta.url))
}

function readJson(file) {
  try {
    const value = JSON.parse(readFileSync(file, 'utf8'))
    return value && typeof value === 'object' ? value : null
  } catch {
    return null
  }
}

function writeJson(file, value) {
  try {
    mkdirSync(dirname(file), { recursive: true })
    // mode is best effort: POSIX honours 0600, Windows/NTFS only inherits the
    // directory ACL, so the real protection is keeping this under the user profile.
    writeFileSync(file, JSON.stringify(value, null, 2), { mode: 0o600 })
  } catch {
    // A read-only checkout must not break task execution.
  }
}

function readTail(file, limit = 1800) {
  try {
    const text = readFileSync(file, 'utf8')
    return text.length > limit ? text.slice(-limit) : text
  } catch {
    return ''
  }
}

function isAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error?.code === 'EPERM'
  }
}

function delay(ms, signal) {
  return new Promise((resolveDelay, reject) => {
    if (signal?.aborted) return reject(signal.reason)
    const abort = () => {
      clearTimeout(timer)
      reject(signal.reason)
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort)
      resolveDelay()
    }, ms)
    signal?.addEventListener('abort', abort, { once: true })
  })
}

function layout(config) {
  const configuredRoot = config.repoRoot || process.env.DSH_DESKTOP_REPO
  const root = resolve(configuredRoot || packageRoot())
  const installedRoot = join(process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local'), 'dsh-desktop')
  const pick = (value, fallback) =>
    typeof value === 'string' && value.trim() ? resolve(root, value.trim()) : fallback
  // Kernel state and logs live in the user's DSH tree, not inside the kernel
  // checkout: a published plugin must not need write access to someone's repo.
  // `logDir` is the pre-P1 name and is still honoured so old profiles keep working.
  const stateOverride = (config.stateDir || config.logDir || '').trim()
  const state = stateOverride ? resolve(root, stateOverride) : join(homedir(), '.dsh', 'desktop')
  return {
    root,
    state,
    legacy: join(root, '.dsh-desktop-local'),
    python: pick(config.pythonPath, join(root, '.venv', 'Scripts', 'python.exe')),
    installedPython: join(installedRoot, 'venv', 'Scripts', 'python.exe'),
    home: pick(config.homeDir, configuredRoot ? join(root, '.dsh-desktop-home') : installedRoot),
  }
}

/**
 * The kernel is Windows-only by construction (pywin32, Win32 UI Automation and
 * screen APIs), so a non-Windows host gets one clear error instead of a search
 * for `.venv/bin/python` that can never work. The plugin layer itself is a plain
 * HTTP client, so `manageKernel: false` still lets it reach a kernel started
 * separately on Windows.
 */
function assertWindowsHost(platform) {
  if (platform === 'win32') return
  throw new Error(
    `Desktop kernel requires Windows (pywin32 + Win32/UIA APIs); this host reports "${platform}". `
    + 'Run DSH on Windows, or set manageKernel=false to talk to a kernel started separately on a Windows host.',
  )
}

export function createKernel(config, options = {}) {
  const paths = layout(config)
  const moduleRoot = resolve(dirname(fileURLToPath(import.meta.url)), 'kernel')
  const platform = options.platform || process.platform
  const stateFile = join(paths.state, 'kernel-state.json')
  const legacyStateFile = join(paths.legacy, 'kernel-state.json')
  const outLog = join(paths.state, 'kernel.out.log')
  const errLog = join(paths.state, 'kernel.err.log')
  const base = `http://127.0.0.1:${config.port}/api/dsh`
  let current = null
  let pending = null
  let child = null

  const envToken = () => {
    const value = process.env[config.tokenEnv]
    return typeof value === 'string' ? value : ''
  }

  /**
   * New location first, then the legacy file inside the kernel checkout.
   * A kernel started by an older plugin keeps its token there; without this
   * fallback the plugin would spawn a second kernel on an occupied port.
   */
  function storedState() {
    for (const [file, source] of [[stateFile, 'state file'], [legacyStateFile, 'legacy state file']]) {
      const record = readJson(file)
      if (record && record.port === config.port) return { record, source }
    }
    return null
  }

  function candidateTokens() {
    const record = storedState()?.record
    const stored = typeof record?.token === 'string' ? record.token : ''
    const tokens = [...new Set([envToken(), stored])].filter((token) => token.length >= 32)
    // With no known token, probe with a throwaway one so an already occupied port
    // is reported as such instead of looking like a quiet port.
    return tokens.length ? tokens : ['probe'.repeat(10)]
  }

  async function probeHealth(token) {
    try {
      const response = await fetch(`${base}/health`, {
        headers: { Authorization: `Bearer ${token}`, 'X-DSH-Owner': HEALTH_OWNER },
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
        redirect: 'error',
      })
      if (!response.ok) {
        await response.body?.cancel().catch(() => {})
        return { kind: 'foreign', token, status: response.status }
      }
      const value = await response.json().catch(() => null)
      if (!value || typeof value.protocol !== 'number') {
        return { kind: 'foreign', token, status: response.status, reason: 'unexpected health payload' }
      }
      if (value.protocol < REQUIRED_PROTOCOL) {
        return { kind: 'outdated', token, protocol: value.protocol, health: value }
      }
      return { kind: 'running', token, health: value }
    } catch (error) {
      if (error?.name === 'TimeoutError' || error?.name === 'AbortError') {
        return { kind: 'foreign', token, reason: 'health probe timed out' }
      }
      return { kind: 'absent' }
    }
  }

  async function probe() {
    let blocked = null
    let outdated = null
    for (const token of candidateTokens()) {
      const result = await probeHealth(token)
      if (result.kind === 'running') return result
      // One stale token must not mask a working one further down the list.
      if (result.kind === 'outdated') outdated = outdated || result
      else if (result.kind === 'foreign') blocked = blocked || result
    }
    return outdated || blocked || { kind: 'absent' }
  }

  function failure(message) {
    const tail = readTail(errLog)
    return new Error(`Desktop ${message}; log: ${errLog}${tail ? `\n${tail}` : ''}`)
  }

  function reconcileStatePid(pid, token) {
    if (!Number.isInteger(pid) || pid <= 0) return
    const stored = readJson(stateFile)
    // Never replace another kernel's credentials or rewrite environment-only state.
    if (stored?.port === config.port && stored.token === token && stored.pid !== pid) {
      writeJson(stateFile, { ...stored, pid })
    }
  }

  function attach(handle, token, own, health, source) {
    current = { token, pid: Number.isInteger(handle) ? handle : null, own, health, source }
    // Own kernels always record their token. An adopted legacy record is rewritten
    // into the new location, so the compatibility read is needed exactly once.
    if (own || source === 'legacy state file') {
      writeJson(stateFile, {
        port: config.port,
        token,
        pid: current.pid,
        startedAt: new Date().toISOString(),
        source: own ? 'dsh-plugin' : 'adopted-from-legacy',
      })
    } else if (source === 'state file') {
      reconcileStatePid(health?.bridge?.pid, token)
    }
    return current
  }

  /** Kernel version, for error messages only: read from the checkout when present. */
  function kernelVersion() {
    try {
      const text = readFileSync(join(moduleRoot, 'dsh_desktop', 'version.py'), 'utf8')
      const found = text.match(/__version__\s*=\s*['"]([^'"]+)['"]/)
      if (found) return found[1]
    } catch {}
    try {
      const text = readFileSync(join(paths.root, 'pyproject.toml'), 'utf8')
      const found = text.match(/^version\s*=\s*["']([^"']+)["']/m)
      if (found) return found[1]
    } catch {}
    return ''
  }

  function assertProtocol(health, where) {
    const protocol = health?.protocol
    if (typeof protocol === 'number' && protocol >= REQUIRED_PROTOCOL) return
    const version = kernelVersion() || 'unknown version'
    throw new Error(
      `Desktop kernel ${version} speaks protocol ${protocol ?? 'none'}, but this plugin requires ${REQUIRED_PROTOCOL} (${where}). `
      + 'Update the DSH plugin package and retry.',
    )
  }

  /**
   * The Python runtime is installed separately; the kernel source ships in this plugin.
   */
  function executable() {
    const explicit = typeof config.pythonPath === 'string' && config.pythonPath.trim()
    if (explicit) {
      if (!existsSync(paths.python)) {
        throw new Error(`pythonPath points at ${paths.python}, which does not exist; fix the plugin config or unset it`)
      }
      return { command: paths.python, args: ['-m', 'dsh_desktop.dsh_bridge_cli'], source: 'config.pythonPath' }
    }
    if (existsSync(paths.python)) {
      return { command: paths.python, args: ['-m', 'dsh_desktop.dsh_bridge_cli'], source: 'repoRoot venv' }
    }
    if (existsSync(paths.installedPython)) {
      return { command: paths.installedPython, args: ['-m', 'dsh_desktop.dsh_bridge_cli'], source: 'bootstrap venv' }
    }
    throw new Error(
      `Desktop kernel Python environment not found: tried ${paths.python} and ${paths.installedPython}. `
      + 'Run the bundled scripts/bootstrap.ps1 or set repoRoot/pythonPath in the plugin config.',
    )
  }

  /**
   * The key the kernel runs with: the credential configured in this DSH window
   * first (it outranks environment fallbacks there too), then the process
   * environment, then an optional local file.
   */
  async function resolveApiKey() {
    if (typeof options.resolveApiKey === 'function') {
      try {
        const value = await options.resolveApiKey()
        if (typeof value === 'string' && value.trim()) return { key: value.trim(), source: 'DSH credentials' }
      } catch {
        // A missing credentials service must not block an environment or file key.
      }
    }
    const inherited = process.env.DEEPSEEK_API_KEY
    if (typeof inherited === 'string' && inherited.trim()) return { key: inherited.trim(), source: 'environment' }
    const explicit = typeof config.apiKeyFile === 'string' && config.apiKeyFile.trim()
    const candidates = explicit
      ? [resolve(paths.root, config.apiKeyFile.trim())]
      : [join(paths.state, DEFAULT_KEY_FILE), join(paths.legacy, DEFAULT_KEY_FILE)]
    for (const keyFile of candidates) {
      if (!existsSync(keyFile)) continue
      try {
        const value = readFileSync(keyFile, 'utf8').trim()
        if (value) return { key: value, source: keyFile }
      } catch {}
    }
    return { key: '', source: '' }
  }

  // 高权限启动用的计划任务名。由 scripts/install-elevated-task.ps1 注册
  // （RunLevel=Highest）。注册需要一次性管理员权限，之后每次触发都不需要 UAC。
  const ELEVATED_TASK_NAME = 'DshDesktopKernelAdmin'
  const ELEVATED_BOOT_FILE = 'kernel-boot.json'

  /**
   * 尝试用已注册的高权限计划任务启动内核。
   *
   * 返回 { started: true } 表示任务已触发（不代表内核已起来，调用方仍要轮询 health）；
   * 返回 { started: false, reason } 表示跳过（任务未注册 / schtasks 不可用）。
   *
   * 参数通过 kernel-boot.json 传给任务脚本，所以插件换 token 或端口后依然有效，
   * 不需要重新注册任务。
   */
  async function tryStartViaElevatedTask({ token, apiKey }) {
    if (platform !== 'win32') return { started: false, reason: 'not windows' }
    try {
      const query = spawnSync('schtasks', ['/Query', '/TN', ELEVATED_TASK_NAME], {
        windowsHide: true,
        encoding: 'utf8',
        timeout: 15000,
      })
      if (query.status !== 0) {
        return { started: false, reason: `task ${ELEVATED_TASK_NAME} not registered` }
      }
      const bootFile = join(paths.home, ELEVATED_BOOT_FILE)
      try {
        writeFileSync(
          bootFile,
          JSON.stringify({ token, port: config.port, home: paths.home, stateDir: paths.state, apiKey: apiKey || '' }),
          'utf8',
        )
      } catch (error) {
        return { started: false, reason: `cannot write ${ELEVATED_BOOT_FILE}: ${error.message}` }
      }
      // The file carries the bridge token and possibly the model API key in
      // plaintext. It has to be readable by the elevated task, so the ACL is
      // narrowed to the current user plus the two built-in service principals
      // (SIDs instead of names, so the shipped file stays free of local words).
      try {
        const who = process.env.USERNAME || ''
        spawnSync('icacls', [bootFile, '/inheritance:r',
          '/grant:r', `${who}:(F)`, '*S-1-5-18:(F)', '*S-1-5-32-544:(F)'],
        { windowsHide: true, timeout: 15000 })
      } catch {
        // Non-fatal: the task may still read it through inherited rights.
      }
      const run = spawnSync('schtasks', ['/Run', '/TN', ELEVATED_TASK_NAME], {
        windowsHide: true,
        encoding: 'utf8',
        timeout: 15000,
      })
      if (run.status !== 0) {
        return { started: false, reason: `schtasks /Run failed: ${(run.stderr || run.stdout || '').trim()}` }
      }
      return { started: true }
    } catch (error) {
      return { started: false, reason: error.message }
    }
  }

  async function spawnKernel(signal) {
    // Only the "start it ourselves" path needs Windows: adopting a kernel that is
    // already answering on loopback stays a plain HTTP call, so the diagnosis
    // matches the real problem (it cannot start here) instead of the host name.
    assertWindowsHost(platform)
    const launch = executable()
    mkdirSync(paths.state, { recursive: true })
    const token = envToken().length >= 32 ? envToken() : randomBytes(48).toString('base64url')
    const env = {
      ...process.env,
      [config.tokenEnv]: token,
      DSH_DESKTOP_HOME: paths.home,
      PYTHONPATH: [moduleRoot, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
    }
    const { key: apiKey, source: keySource } = await resolveApiKey()
    if (apiKey) env.DEEPSEEK_API_KEY = apiKey

    // 优先用高权限计划任务启动内核。
    // 原因（实测 2026-10-27）：spawn 继承的是插件的权限（通常 Medium），
    // 而目标应用常是管理员启动的（可能处于 High 完整性级别），
    // Medium 内核会被 UIPI 全面阻断：topmost / activate / click / type 全失败，
    // 但截图和 OCR 仍然可用 —— 表现为「读能用、写全废」，很难归因。
    // 任务由 scripts/install-elevated-task.ps1 一次性注册；没注册就安静跳过。
    const elevated = await tryStartViaElevatedTask({ token, apiKey })
    if (elevated.started) {
      const deadline = Date.now() + config.bootSeconds * 1000
      while (Date.now() < deadline) {
        signal?.throwIfAborted()
        const result = await probeHealth(token)
        if (result.kind === 'running') {
          // 计划任务拿不到 child.pid，从 health 里取（内核自报）
          const pid = result.health?.bridge?.pid
          const stored = attach(pid, token, true, result.health, 'plugin autostart (elevated task)')
          console.info(
            '[desktop] kernel started via elevated task (pid=%s, port=%d, api key from %s)',
            pid, config.port, keySource || 'nowhere; execution disabled',
          )
          return stored
        }
        if (result.kind === 'outdated') {
          assertProtocol(result.health, 'port already in use by an older kernel')
        }
        if (result.kind === 'foreign') {
          throw failure(`kernel answered HTTP ${result.status ?? result.reason} during startup`)
        }
        await delay(POLL_INTERVAL_MS, signal)
      }
      // 任务没能把内核拉起来：退回普通 spawn，并要求调用方去查任务日志
      console.warn(
        '[desktop] elevated task did not bring the kernel up within %ds; falling back to spawn',
        config.bootSeconds,
      )
    } else if (elevated.reason) {
      // 未注册是最常见的情况，用 info 而不是 warn，避免正常部署时刷警告
      console.info('[desktop] elevated start skipped: %s', elevated.reason)
    }

    const out = openSync(outLog, 'a')
    const err = openSync(errLog, 'a')
    let exited = ''
    try {
      child = spawn(
        launch.command,
        [...launch.args, '--home-dir', paths.home, '--port', String(config.port)],
        { cwd: paths.root, env, windowsHide: true, detached: true, stdio: ['ignore', out, err] },
      )
    } finally {
      closeSync(out)
      closeSync(err)
    }
    child.on('error', (error) => { exited = exited || `could not launch ${launch.command}: ${error.message}` })
    child.on('exit', (code, signalName) => {
      exited = exited || `kernel exited (code=${code}${signalName ? `, signal=${signalName}` : ''})`
    })
    // Detached and unreferenced: DSH can quit without taking the kernel down.
    child.unref()
    const pid = child.pid

    const deadline = Date.now() + config.bootSeconds * 1000
    while (Date.now() < deadline) {
      signal?.throwIfAborted()
      if (exited) throw failure(exited)
      const result = await probeHealth(token)
      if (result.kind === 'running') {
        const stored = attach(pid, token, true, result.health, 'plugin autostart')
        console.info('[desktop] kernel started (pid=%d, port=%d, api key from %s)', pid, config.port, keySource || 'nowhere; execution disabled')
        return stored
      }
      if (result.kind === 'outdated') {
        // Our own fresh process cannot be outdated: another kernel holds the port.
        assertProtocol(result.health, 'port already in use by an older kernel')
      }
      if (result.kind === 'foreign') {
        throw failure(`kernel answered HTTP ${result.status ?? result.reason} during startup`)
      }
      await delay(POLL_INTERVAL_MS, signal)
    }
    throw failure(`kernel did not become ready within ${config.bootSeconds}s`)
  }

  async function boot(signal) {
    const found = await probe()
    if (found.kind === 'outdated') assertProtocol(found.health, 'kernel already listening')
    if (found.kind === 'running') {
      const stored = storedState()
      const pid = Number.isInteger(found.health?.bridge?.pid)
        ? found.health.bridge.pid : stored?.record?.pid
      const source = found.token === envToken() ? 'environment' : stored?.source ?? 'state file'
      const adopted = attach(pid, found.token, false, found.health, source)
      // A kernel that started without a key can never have accepted a task (the
      // bridge refuses submissions), so replacing it is safe and is the only way
      // a key configured after that start can take effect.
      if (!found.health.execution_enabled && source !== 'environment') {
        const { key, source: keySource } = await resolveApiKey()
        if (key) {
          console.info('[desktop] adopted kernel has no API key; restarting it with %s', keySource)
          await stop({ force: true })
          return spawnKernel(signal)
        }
      }
      return adopted
    }
    if (found.kind === 'foreign') {
      throw new Error(
        `Port ${config.port} is already serving a Desktop bridge that rejects this token (${found.status ?? found.reason}); close that kernel or the Desktop app, then retry`,
      )
    }
    if (!config.autoStart) {
      throw new Error(`The Desktop kernel is not running on 127.0.0.1:${config.port}; start it or set autoStart=true`)
    }
    signal?.throwIfAborted()
    return spawnKernel(signal)
  }

  async function ensure(signal) {
    if (current) return current
    if (!pending) pending = boot(signal).finally(() => { pending = null })
    return pending
  }

  function forget() {
    current = null
    child = null
  }

  function observeHealth(health) {
    if (!current || !health) return
    const pid = health?.bridge?.pid
    if (Number.isInteger(pid) && pid > 0 && pid !== current.pid) {
      // Another kernel replaced the old one. Treat it as adopted, not owned.
      current.pid = pid
      current.own = false
      reconcileStatePid(pid, current.token)
    }
    current.health = health
  }

  function describe() {
    if (!current) return null
    return {
      port: config.port,
      token_source: current.source,
      pid: current.pid,
      started_by_plugin: current.own,
      execution_enabled: Boolean(current.health?.execution_enabled),
      protocol: current.health?.protocol ?? null,
      log: errLog,
    }
  }

  async function stop({ force = false } = {}) {
    const target = current
    current = null
    if (!target) return false
    // A kernel inherited from the environment (started by the user's script) is never killed.
    const killable = target.own || (force && target.source !== 'environment')
    if (!killable || !isAlive(target.pid)) return false
    // A stale or recycled PID must never identify the process to terminate.
    const live = await probeHealth(target.token)
    if (live.kind !== 'running' || live.health?.bridge?.pid !== target.pid) return false
    await killTree(target.pid)
    if (isAlive(target.pid)) return false
    try { unlinkSync(stateFile) } catch {}
    // Only drop the legacy record if it describes this kernel, never someone else's.
    if (readJson(legacyStateFile)?.port === config.port) {
      try { unlinkSync(legacyStateFile) } catch {}
    }
    return true
  }

  return {
    ensure,
    stop,
    forget,
    observeHealth,
    describe,
    executable,
    get ready() { return Boolean(current) },
    get token() { return current?.token ?? envToken() },
    paths,
  }
}

/**
 * A uv-managed venv puts a trampoline at .venv/Scripts/python.exe and runs the
 * real interpreter as its child, so the console process is not enough: kill the tree.
 */
function killTree(pid) {
  return new Promise((done) => {
    if (process.platform !== 'win32') {
      try { process.kill(pid) } catch {}
      return done()
    }
    const killer = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
    killer.on('error', () => {
      try { process.kill(pid) } catch {}
      done()
    })
    killer.on('exit', () => done())
  })
}
