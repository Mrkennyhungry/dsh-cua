import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import { createWriteStream } from 'node:fs'
import { homedir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { performance } from 'node:perf_hooks'
import { startFixtures, verifyFixture } from '../test/benchmark-fixtures.mjs'
import { bridgeRequest, browserProcesses, cancellationComplete, cleanTrial, startModelRecorder } from './benchmark-transport.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const options = new Map(process.argv.slice(2).map(value => {
  const at = value.indexOf('=')
  return at < 0 ? [value, true] : [value.slice(0, at), value.slice(at + 1)]
}))
const model = options.get('--model') || 'cn:deepseek-v4.1-flash'
if (!['cn:deepseek-v4.1-flash', 'cn:hy4std'].includes(model)) throw new Error('Unknown approved model route')
const version = options.get('--version') || JSON.parse(await readFile(join(root, 'package.json'))).version
const legacy = options.has('--legacy')
const profile = options.get('--profile') || (legacy ? 'cua-p7-legacy' : 'headless')
const count = Number(options.get('--attempts') || 3)
const timeoutMs = Number(options.get('--timeout-ms') || 300000)
if (!Number.isInteger(timeoutMs) || timeoutMs < 1000) throw new Error('timeout-ms must be a positive integer of at least 1000')
if (!Number.isInteger(count) || count < 1) throw new Error('attempts must be a positive integer')
const tasks = String(options.get('--tasks') || 'form,upload,frames,toast,notepad,concurrency,persistence').split(',')
const supported = ['form', 'upload', 'frames', 'toast', 'cancellation', 'notepad', 'concurrency', 'persistence']
if (tasks.some(task => !supported.includes(task))) throw new Error(`Supported tasks are ${supported.join(', ')}`)
const output = resolve(options.get('--output') || '.runtime/p7-benchmark')
await mkdir(output, { recursive: true })
if ((await readdir(output)).length) throw new Error('Use an empty benchmark output directory to preserve existing attempts')
const fixtures = await startFixtures()
const recorder = await startModelRecorder()
const executable = options.get('--electron') || join(process.env.LOCALAPPDATA, 'Programs/DeepSeek Harness/DeepSeek Harness.exe')
const bridgeState = resolve(options.get('--bridge-state') || join(homedir(), '.dsh/desktop/kernel-state.json'))
const port = Number(options.get('--port') || 8766)
const browserProfile = resolve(options.get('--browser-profile') || join(process.env.LOCALAPPDATA, 'dsh-desktop/web_profile_edge')).toLowerCase()
const ownedBrowserProcesses = async () => (await browserProcesses()).filter(process => resolve(process.profile).toLowerCase() === browserProfile)
const identity = legacy ? await (async () => {
  const state = JSON.parse(await readFile(bridgeState, 'utf8'))
  const response = await fetch(`http://127.0.0.1:${port}/api/dsh/health`, { headers: {
    Authorization: `Bearer ${state.token}`, 'X-DSH-Owner': '0'.repeat(64),
  }, signal: AbortSignal.timeout(5000) })
  const health = await response.json()
  if (!response.ok || health.protocol !== 1) throw new Error('Expected legacy protocol 1')
  const installed = JSON.parse(await readFile(join(root, '.runtime/p7-legacy/package.json')))
  if (installed.version !== version || !health.bridge?.source_root?.includes('p7-legacy')) throw new Error('Legacy source identity mismatch')
  return health
})() : await bridgeRequest(bridgeState, port, 'p7-identity', '/runtime/status', {})
if (!legacy && identity.version !== version) throw new Error(`Expected runtime ${version}, received ${identity.version}`)
const patch = join(output, 'launch.yml')
const maxTokens = model === 'cn:hy4std' ? 64000 : 393216
await writeFile(patch, `- id: credentials
  disabled: false
- id: agent-default-model
  config:
    provider: wb
    model: ${model}
- id: llm-pi-ai
  config:
    providers:
      wb:
        displayName: wb
        apiKeyEnv: WB_API_KEY
        api: openai-completions
        baseURL: ${recorder.url}
        models:
          - id: ${model}
            contextWindow: 1000000
            maxTokens: ${maxTokens}
            input: [text, image]
- id: doraemon
  config:
    port: ${options.get('--port') || 8766}
    manageKernel: false
    keepAlive: true
    companionSelection: '[]'
    commandFeedback: false
`)

const header = '这是本地开发验收。仅使用本会话已注册的 doraemon 工具操作 fixture，遵循实际工具参数和观察结果。不要使用代码、文件、终端或网络工具代替界面操作。不要读取源码。遇到失败保留真实原因。完成后简短报告结果。'
const reports = []

function promptFor(task, id, file) {
  const page = task === 'frames' ? 'tabs' : task === 'concurrency' ? 'cancellation' : task
  const url = `${fixtures.origin}/run/${id}/${page}`
  const descriptions = {
    form: `打开 ${url}。将备注替换为 P7-${id}，测试口令填入 fixture-only-42，勾选加急，城市选择上海，然后提交保存表单恰好一次。读回所有字段和已保存结果。`,
    upload: `打开 ${url}。通过附件控件选择本地测试文件 ${file}。读回页面显示的文件名和完整 SHA256。文件只发送到本机 fixture。`,
    frames: `打开 ${url}。点击“打开第二个相同 URL 标签”，在标识为 P7_SECOND_${id} 的第二个标签里，点击 outer 内 inner frame 中的“重复按钮”恰好一次，读回 inner done。`,
    toast: `打开 ${url}。捕捉点击“显示短提示”产生的120毫秒提示 P7 transient saved。要求动作触发前开始监听，动作结果保留命中证据，再查询该动作状态确认已存证据。仅触发一次。`,
    cancellation: `打开 ${url}。将会话归属替换为 A-${id} 并保存一次。然后等待文本 P7_NEVER_ARRIVES（30000毫秒），取得后台操作句柄后立即取消它，查询到取消完成后结束。取消后不要输入或点击。`,
    concurrency: `打开 ${url}。将会话归属替换为 A-${id} 并保存一次。然后等待文本 P7_NEVER_ARRIVES（30000毫秒），取得后台操作句柄后立即取消它，查询到取消完成后结束。取消后不要输入或点击。另一独立 DSH 会话正在运行，取消只限自己的操作。`,
    notepad: `用记事本打开已存在的本次测试文件 ${file}，将全部正文替换为 P7-${id}，按 Ctrl+S 保存，读回正文和未修改状态后结束。只操作这个文件标签。不要打开最近文件或其他文档，也不需要关闭标签。`,
    persistence: `打开 ${url}。点击“设置 fixture 登录态”，读回 P7_LOGIN=${id} 和站点设置 ${id}。随后正常关闭标题含 P7 ${id} 的专用浏览器原生窗口，使浏览器退出。保留 profile。此专用测试浏览器的所有标签均属于本轮 fixture。结束时不要重新打开浏览器。`,
  }
  return `${header}\n${descriptions[task]}`
}

function runDsh(prompt, logPath, onEvent = () => {}) {
  return new Promise(resolveRun => {
    const started = performance.now()
    const child = spawn(executable, [join(root, 'scripts/dsh-benchmark-entry.mjs'),
      '--profile', profile, '--patch', patch, '--json', prompt], {
      cwd: root, windowsHide: true,
      env: { ...process.env, ELECTRON_RUN_AS_NODE: '1', DSH_BENCHMARK_BRIDGE_STATE: bridgeState },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const events = []
    const log = createWriteStream(logPath)
    let buffer = ''
    let diagnostics = ''
    let timedOut = false
    const timer = setTimeout(async () => {
      timedOut = true
      const session = events.find(event => event.type === 'session')?.sessionId
      if (session && !legacy) await bridgeRequest(bridgeState, port, session, '/runtime/stop', {}).catch(error => { diagnostics += error.message })
      spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], { windowsHide: true, stdio: 'ignore' })
    }, timeoutMs)
    child.stdout.setEncoding('utf8').on('data', chunk => {
      buffer += chunk
      const lines = buffer.split('\n')
      buffer = lines.pop()
      for (const line of lines) {
        try {
          const event = { received_ms: +(performance.now() - started).toFixed(3), ...JSON.parse(line) }
          events.push(event)
          log.write(JSON.stringify(event) + '\n')
          onEvent(event, events)
        }
        catch { diagnostics += line + '\n' }
      }
    })
    child.stderr.setEncoding('utf8').on('data', chunk => { diagnostics += chunk })
    child.on('error', error => { diagnostics += error.message })
    child.on('close', async code => {
      clearTimeout(timer)
      const duration_ms = +(performance.now() - started).toFixed(3)
      await new Promise(resolveLog => log.end(resolveLog))
      await writeFile(logPath + '.stderr', diagnostics + buffer)
      resolveRun({ code, timedOut, duration_ms, events })
    })
  })
}

function summarize(run) {
  const calls = run.events.filter(event => event.type === 'tool_call')
  const results = run.events.filter(event => event.type === 'tool_result')
  const samples = calls.flatMap(call => {
    const result = results.find(event => event.callId === call.callId)
    return result ? [{ name: call.tool, ms: +(result.received_ms - call.received_ms).toFixed(3) }] : []
  })
  const sorted = samples.map(sample => sample.ms).sort((a, b) => a - b)
  const steps = run.events.filter(event => event.phase === 'step_end')
  const modelRequests = run.events.filter(event => event.phase === 'step_start').length
  const usage = {}
  const usageObserved = {}
  for (const key of ['inputTokens', 'outputTokens', 'totalTokens', 'cacheReadTokens', 'cacheWriteTokens']) {
    usageObserved[key] = steps.length > 0 && steps.every(step => typeof step.usage?.[key] === 'number')
      ? steps.reduce((sum, step) => sum + step.usage[key], 0) : null
    usage[key] = steps.length > 0 && steps.length === modelRequests
      && steps.every(step => typeof step.usage?.[key] === 'number')
      ? steps.reduce((sum, step) => sum + step.usage[key], 0) : null
  }
  const final = run.events.findLast(event => event.type === 'final')
  const values = results.map(event => {
    try { return JSON.parse(event.result) } catch { return null }
  })
  const failed = results.filter((event, index) => event.status !== 'completed'
    || values[index]?.success === false || values[index]?.error).length
  const images = []
  for (const value of values) {
    const observation = value?.observation || value?.result?.observation
    for (const frame of observation?.frames || []) images.push({ width: frame.width, height: frame.height })
  }
  return {
    session_id: run.events.find(event => event.type === 'session')?.sessionId || null,
    process_exit_code: run.code, timed_out: run.timedOut, end_to_end_ms: run.duration_ms,
    model_requests: modelRequests,
    tools: calls.length, failed_tools: failed, samples,
    tool_p50_ms: sorted[Math.ceil(sorted.length * .5) - 1] ?? null,
    tool_p95_ms: sorted[Math.ceil(sorted.length * .95) - 1] ?? null,
    usage, usage_observed: usageObserved, usage_complete: steps.length > 0 && steps.length === modelRequests,
    images, image_pixels: images.reduce((sum, frame) => sum + frame.width * frame.height, 0),
    final_reason: final?.reason || null, answer: final?.text || '',
    exact_cost: null, separate_image_cost: null,
    tool_values: values,
  }
}

try {
  for (const task of tasks) {
    for (let attempt = 1; attempt <= count; attempt += 1) {
      const id = randomUUID().replaceAll('-', '').slice(0, 12)
      const file = join(output, `p7-${id}.txt`)
      const bytes = Buffer.from(task === 'notepad' ? '' : `P7 owned upload fixture ${id}\n`, 'utf8')
      await writeFile(file, bytes)
      const trial = fixtures.add(id, bytes)
      const startingState = legacy ? null : await bridgeRequest(bridgeState, port, `p7-before-${id}`, '/runtime/status', {})
      const requestStart = recorder.records.length
      const started = performance.now()
      let peer, peerRun, cancelledAt, openedProcesses
      const cancellationEvidence = []
      if (task === 'concurrency') peer = fixtures.add(id + 'b', bytes)
      const ownPromise = runDsh(promptFor(task, id, file), join(output, `${task}-${attempt}.jsonl`), (event, events) => {
        if (task === 'persistence' && event.type === 'tool_result') {
          openedProcesses = Promise.all([openedProcesses || [], ownedBrowserProcesses()]).then(([before, current]) =>
            [...new Map([...before, ...current].map(process => [process.pid, process])).values()])
        }
        if (legacy && task === 'toast' && event.type === 'tool_result') {
          const call = events.find(row => row.type === 'tool_call' && row.callId === event.callId)
          try {
            const value = JSON.parse(event.result)
            if (call?.tool === 'doraemon_watch' && call.input?.action === 'start'
              && value.success === true && value.active === true) trial.watchStartedAt = Date.now()
          } catch {}
        }
        if (!peer || event.type !== 'tool_result') return
        try {
          cancellationEvidence.push(JSON.parse(event.result))
          if (cancellationComplete(cancellationEvidence, legacy)) {
            cancelledAt ??= Date.now()
            peer.unlocked = true
          }
        } catch {}
      })
      if (peer) {
        peerRun = await runDsh(`${header}\n打开 ${fixtures.origin}/run/${id}b/cancellation_b。将会话归属替换为 B-${id}。等待“取消方已停止”出现和保存按钮启用后，保存归属恰好一次，读回已保存并结束。`, join(output, `${task}-${attempt}-peer.jsonl`))
      }
      const ownRun = await ownPromise
      let restoredRun
      let persistence
      if (task === 'persistence') {
        const opened = await openedProcesses || []
        const closed = await ownedBrowserProcesses()
        persistence = { opened, closed,
          process_exit: opened.length > 0 && opened.every(before => !closed.some(after => after.pid === before.pid)) }
        const before = trial.events.length
        let restoredProcesses = Promise.resolve([])
        restoredRun = await runDsh(`${header}\n重新打开 ${fixtures.origin}/run/${id}/persistence。页面正文直接展示 Cookie 和站点设置，读到 P7_LOGIN=${id} 与站点设置 ${id} 即完成自动恢复核验。不要点击设置登录态。随后点击“模拟登录失效”读回需用户重新登录的状态，再点击“模拟验证码”读回用户介入提示，并报告当前可继续的目标后结束。`, join(output, `${task}-${attempt}-restored.jsonl`), event => {
          if (event.type === 'tool_result') restoredProcesses = Promise.all([restoredProcesses, ownedBrowserProcesses()])
            .then(([before, current]) => [...new Map([...before, ...current].map(process => [process.pid, process])).values()])
        })
        trial.restored = trial.events.slice(before).some(event => event.kind === 'restore'
          && event.cookie.includes(`P7_LOGIN=${id}`) && event.site === id)
        persistence.reopened = await restoredProcesses
        persistence.same_profile = persistence.reopened.some(after => opened.some(before => before.profile === after.profile && before.pid !== after.pid))
      }
      const additional = peerRun || restoredRun
      const run = additional ? { ...ownRun, duration_ms: +(performance.now() - started).toFixed(3),
        events: [...ownRun.events, ...additional.events], code: ownRun.code || additional.code,
        timedOut: ownRun.timedOut || additional.timedOut } : ownRun
      const summary = summarize(run)
      const business = peer ? verifyFixture('cancellation', trial, id)
        && peer.events.filter(event => event.kind === 'owner').length === 1
        && peer.events.some(event => event.kind === 'owner' && event.value === `B-${id}` && event.at >= cancelledAt)
        && !trial.events.some(event => event.kind === 'input' && event.at > cancelledAt)
        : task === 'persistence' ? trial.restored && persistence.process_exit && persistence.same_profile
          && trial.events.filter(event => event.kind === 'login').length === 1
          && trial.events.filter(event => event.kind === 'expired').length === 1
          && trial.events.filter(event => event.kind === 'captcha').length === 1
        : task === 'notepad' ? (await readFile(file, 'utf8')).replace(/^\uFEFF/, '') === `P7-${id}`
        : verifyFixture(task, trial, id, basename(file))
      const evidence = summary.tool_values.filter(Boolean)
      const transientCalls = run.events.filter(event => event.type === 'tool_call' && event.tool === 'doraemon_act'
        && event.input?.expect?.transient === true)
      const transientOperation = run.events.find(event => event.type === 'tool_result'
        && transientCalls.some(call => call.callId === event.callId))
      let transientValue
      try { transientValue = JSON.parse(transientOperation?.result) } catch {}
      const statusCall = run.events.find(event => event.type === 'tool_call' && event.tool === 'doraemon_status'
        && event.input?.operation_id === transientValue?.operation_id)
      const legacyToast = evidence.some(value => value.evidence_image
        && value.text_matches?.some(match => match.text.includes('P7 transient saved')))
      const observed = task !== 'toast' || (legacy ? legacyToast : transientValue?.expectation?.status === 'met'
        && transientValue.expectation.evidence?.text === 'P7 transient saved'
        && transientValue.observation?.frames?.length > 0
        && run.events.some(event => event.type === 'tool_result' && event.callId === statusCall?.callId
          && JSON.parse(event.result)?.expectation?.status === 'met'
          && JSON.parse(event.result)?.observation?.observation_id === transientValue.observation.observation_id))
      const cancelled = !['cancellation', 'concurrency'].includes(task) || cancellationComplete(evidence, legacy)
      const result = {
        phase: 'P7', version, identity, starting_state: startingState, provider: 'wb', model, task, attempt, fixture_id: id, timeout_ms: timeoutMs,
        status: business && observed && cancelled && run.code === 0
          && run.events.filter(event => event.type === 'final').length === (additional ? 2 : 1)
          && run.events.filter(event => event.type === 'final').every(event => event.reason?.kind === 'completed') ? 'PASS' : 'FAIL',
        fixture_pass: business, evidence_pass: observed && cancelled,
        fixture_events: trial.events, peer_events: peer?.events, cancelled_at: cancelledAt, persistence,
        ...summary, session_ids: run.events.filter(event => event.type === 'session').map(event => event.sessionId),
        provider_requests: recorder.records.slice(requestStart),
      }
      if (peerRun) {
        const peerSession = peerRun.events.find(event => event.type === 'session')?.sessionId
        if (peerSession) result.peer_cleanup = await (legacy
          ? bridgeRequest(bridgeState, port, peerSession, '/cleanup', {
            keep_browser: false, close_launched_apps: false, keep_window_keys: [],
          })
          : cleanTrial(bridgeState, port, peerSession, id + 'b')).catch(error => ({ error: error.message }))
      }
      if (summary.session_id && !legacy) {
        try { result.cleanup = await cleanTrial(bridgeState, port, summary.session_id, id) }
        catch (error) { result.cleanup = { error: error.message } }
      }
      if (restoredRun && !legacy) {
        const restoredSession = restoredRun.events.find(event => event.type === 'session')?.sessionId
        if (restoredSession) result.restored_cleanup = await cleanTrial(bridgeState, port, restoredSession, id)
          .catch(error => ({ error: error.message }))
      }
      if (summary.session_id && legacy) {
        result.cleanup = await bridgeRequest(bridgeState, port, summary.session_id, '/cleanup', {
          keep_browser: false, close_launched_apps: false, keep_window_keys: [],
        }).catch(error => ({ error: error.message }))
      }
      reports.push(result)
      await writeFile(join(output, 'results.json'), JSON.stringify({
        version, provider: 'wb', model, count, tasks, trials: reports,
        limitations: ['Tool times use CLI event arrival timestamps.',
          'Provider requests record native image bytes and hashes. Frame geometry and backend traces require session-log reconciliation.',
          'Exact cost is unavailable from the DSH pi-ai adapter.',
          'Physical mixed-DPI and negative-origin multi-monitor hardware is unavailable on this single-monitor host.'],
      }, null, 2) + '\n')
      console.log(JSON.stringify({ task, attempt, status: result.status, ms: summary.end_to_end_ms, tools: summary.tools }))
    }
  }
} finally {
  await fixtures.close()
  await recorder.close()
}
if (reports.some(trial => trial.status !== 'PASS')) process.exitCode = 1
