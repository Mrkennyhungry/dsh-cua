import test from 'node:test'
import assert from 'node:assert/strict'
import { startFixtures, verifyFixture } from './benchmark-fixtures.mjs'
import { cancellationComplete, imageSize } from '../scripts/benchmark-transport.mjs'

test('benchmark fixtures verify exact values and reject duplicate submissions', async () => {
  const fixtures = await startFixtures()
  try {
    const trial = fixtures.add('owned', Buffer.from('fixture'))
    const page = await fetch(`${fixtures.origin}/run/owned/form`)
    assert.match(await page.text(), /保存表单/)
    const event = { kind: 'form', note: 'P7-owned', password: 'fixture-only-42', urgent: true, city: '上海' }
    const post = () => fetch(`${fixtures.origin}/run/owned/event`, { method: 'POST', body: JSON.stringify(event) })
    await post()
    assert.equal(verifyFixture('form', trial, 'owned'), true)
    await post()
    assert.equal(verifyFixture('form', trial, 'owned'), false)
    trial.events = [{ kind: 'frame', tab: 'FIRST', level: 'inner' }]
    assert.equal(verifyFixture('frames', trial, 'owned'), false)
    trial.events[0].tab = 'P7_SECOND_owned'
    assert.equal(verifyFixture('frames', trial, 'owned'), true)
    trial.events = [{ kind: 'upload', name: 'fixture.txt', sha256: trial.sha256 }]
    assert.equal(verifyFixture('upload', trial, 'owned', 'fixture.txt'), true)
    trial.events[0].sha256 = 'wrong'
    assert.equal(verifyFixture('upload', trial, 'owned', 'fixture.txt'), false)
    trial.events = [{ kind: 'toast', at: Date.now(), watcher_armed: false }]
    assert.equal(verifyFixture('toast', trial, 'owned'), false)
    trial.events[0].watcher_armed = true
    trial.events[0].watcher_dispatched_at = new Date(trial.events[0].at).toISOString()
    assert.equal(verifyFixture('toast', trial, 'owned'), true)
    trial.events[0].watcher_armed = false
    trial.events[0].native_watcher_started_at = trial.events[0].at - 1
    assert.equal(verifyFixture('toast', trial, 'owned'), true)
    trial.events[0].native_watcher_started_at = trial.events[0].at + 1
    assert.equal(verifyFixture('toast', trial, 'owned'), false)
  } finally { await fixtures.close() }
})

test('provider image dimensions come from encoded image headers', () => {
  const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 2, 0xff, 0xc0, 0, 7, 8, 0x03, 0x56, 0x05, 0x56])
  assert.deepEqual(imageSize(jpeg), { width: 1366, height: 854 })
  assert.equal(imageSize(jpeg.subarray(0, 10)), null)
  assert.equal(imageSize(Buffer.from('fixture')), null)
})

test('benchmark cancellation requires execution end under each actual protocol', () => {
  assert.equal(cancellationComplete([{ status: 'cancelled', worker_running: false }]), true)
  assert.equal(cancellationComplete([{ status: 'cancelled', worker_running: true }]), false)
  const request = { task_id: 'a', stop_requested: true, status: 'running' }
  const ended = { task_id: 'a', status: 'finished', result: { status: 'stopped', reason: 'interrupted' } }
  assert.equal(cancellationComplete([request], true), false)
  assert.equal(cancellationComplete([ended], true), false)
  assert.equal(cancellationComplete([request, { ...ended, task_id: 'b' }], true), false)
  assert.equal(cancellationComplete([request, { ...ended, result: { status: 'completed' } }], true), false)
  assert.equal(cancellationComplete([request, ended], true), true)
})
