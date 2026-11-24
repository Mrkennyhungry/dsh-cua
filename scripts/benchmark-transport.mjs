import { createHash } from 'node:crypto'
import { createServer, request as httpRequest } from 'node:http'
import { readFile } from 'node:fs/promises'
import { performance } from 'node:perf_hooks'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

export async function browserProcesses() {
  const { stdout } = await promisify(execFile)('powershell', ['-NoProfile', '-Command',
    "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('msedge.exe','chrome.exe') -and $_.CommandLine -match '--user-data-dir' -and $_.CommandLine -notmatch '--type=' } | ForEach-Object { if ($_.CommandLine -match '(?:\"--user-data-dir=([^\"]+)\"|--user-data-dir=\"([^\"]+)\"|--user-data-dir=([^ ]+))') { [pscustomobject]@{pid=$_.ProcessId;profile=($Matches[1] + $Matches[2] + $Matches[3])} } } | ConvertTo-Json -Compress"],
  { windowsHide: true, timeout: 10000 })
  const value = stdout.trim() ? JSON.parse(stdout) : []
  return Array.isArray(value) ? value : [value]
}

export function imageSize(data) {
  if (data?.length >= 24 && data.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
    return { width: data.readUInt32BE(16), height: data.readUInt32BE(20) }
  }
  if (!data || data.length < 4 || data.readUInt16BE(0) !== 0xffd8) return null
  let offset = 2
  while (offset + 4 < data.length && data[offset] === 0xff) {
    const marker = data[offset + 1]
    const length = data.readUInt16BE(offset + 2)
    if (length < 2 || offset + 2 + length > data.length) return null
    if ([0xc0, 0xc1, 0xc2].includes(marker) && length >= 7) {
      return { width: data.readUInt16BE(offset + 7), height: data.readUInt16BE(offset + 5) }
    }
    offset += length + 2
  }
  return null
}

// Record payload shape at the provider boundary, keeping credentials and image
// contents out of evidence. Requests still reach the configured local wb route.
export async function startModelRecorder() {
  const records = []
  const server = createServer(async (incoming, outgoing) => {
    const chunks = []
    for await (const chunk of incoming) chunks.push(chunk)
    const bytes = Buffer.concat(chunks)
    const started = performance.now()
    const record = { at: Date.now(), bytes: bytes.length, images: [] }
    try {
      const body = JSON.parse(bytes)
      record.model = body.model
      record.temperature = body.temperature ?? null
      record.reasoning_effort = body.reasoning_effort ?? null
      record.max_tokens = body.max_tokens ?? body.max_completion_tokens ?? null
      record.tools = (body.tools || []).map(tool => tool.function?.name)
      record.request_kind = record.tools.length ? 'agent' : 'auxiliary'
      record.tool_schema_sha256 = createHash('sha256').update(JSON.stringify(body.tools || [])).digest('hex')
      record.system_sha256 = createHash('sha256').update(JSON.stringify(body.messages?.filter(message => message.role === 'system'))).digest('hex')
      for (const message of body.messages || []) {
        for (const part of Array.isArray(message.content) ? message.content : []) {
          if (part.type !== 'image_url') continue
          const url = part.image_url?.url || ''
          const match = url.match(/^data:([^;]+);base64,(.*)$/s)
          const data = match ? Buffer.from(match[2], 'base64') : null
          record.images.push({ media_type: match?.[1] || null, bytes: data?.length ?? null,
            sha256: data ? createHash('sha256').update(data).digest('hex') : null,
            ...imageSize(data),
            detail: part.image_url?.detail ?? null })
        }
      }
    } catch (error) { record.error = error.message }
    records.push(record)
    const upstream = httpRequest({ hostname: '127.0.0.1', port: 7863, path: incoming.url,
      method: incoming.method, headers: { ...incoming.headers, host: '127.0.0.1:7863' } }, response => {
      record.http_status = response.statusCode
      record.first_byte_ms = +(performance.now() - started).toFixed(3)
      outgoing.writeHead(response.statusCode, response.headers)
      response.pipe(outgoing)
      response.on('end', () => { record.duration_ms = +(performance.now() - started).toFixed(3) })
    })
    upstream.on('error', error => {
      record.error = error.message
      if (!outgoing.headersSent) outgoing.writeHead(502)
      outgoing.end()
    })
    outgoing.on('close', () => upstream.destroy())
    upstream.end(bytes)
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  return { records, url: `http://127.0.0.1:${server.address().port}/v1`, close: () => new Promise(resolve => {
    server.close(resolve)
    server.closeAllConnections()
  }) }
}

export function cancellationComplete(values, legacy = false) {
  const requested = new Set()
  for (const value of values) {
    if (!value) continue
    if (!legacy && value.status === 'cancelled' && value.worker_running !== true) return true
    if (!legacy || !value.task_id) continue
    if (value.stop_requested === true) requested.add(value.task_id)
    if (requested.has(value.task_id) && value.status === 'finished'
      && value.result?.status === 'stopped' && value.result?.reason === 'interrupted') return true
  }
  return false
}

export async function bridgeRequest(statePath, port, session, path, body) {
  const state = JSON.parse(await readFile(statePath, 'utf8'))
  const owner = createHash('sha256').update(session).digest('hex')
  const response = await fetch(`http://127.0.0.1:${port}/api/dsh${path}`, {
    method: 'POST', headers: { Authorization: `Bearer ${state.token}`, 'X-DSH-Owner': owner, 'Content-Type': 'application/json' },
    body: JSON.stringify(body), signal: AbortSignal.timeout(15000),
  })
  if (!response.ok) throw new Error(`Benchmark cleanup ${path} returned HTTP ${response.status}`)
  return response.json()
}

export async function cleanTrial(statePath, port, session, fixtureId) {
  const request = (path, body) => bridgeRequest(statePath, port, session, path, body)
  const stopped = await request('/runtime/stop', {})
  const directory = await request('/runtime/observe', { request_key: `cleanup-directory-${fixtureId}`, arguments: { image: false } })
  const targets = (directory.targets || []).filter(target => target.kind === 'browser'
    && target.url?.includes(`/run/${fixtureId}/`) && !/\/(outer|inner)$/.test(target.url))
  const closed = []
  for (const target of targets) {
    const observation = await request('/runtime/observe', {
      request_key: `cleanup-observe-${target.target_id}`, arguments: { target_id: target.target_id, image: false },
    })
    if (!observation.observation) continue
    const result = await request('/runtime/act', { request_key: `cleanup-close-${target.target_id}`,
      arguments: { target_id: target.target_id, observation_id: observation.observation.observation_id, steps: [{ action: 'close' }] } })
    closed.push({ target_id: target.target_id, success: result.success, error: result.error ?? null })
  }
  return { stopped, closed }
}
