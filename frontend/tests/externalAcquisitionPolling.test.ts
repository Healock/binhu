import assert from 'node:assert/strict'
import test from 'node:test'
import {
  EXTERNAL_RUN_POLL_INTERVAL_MS,
  hasActiveExternalAcquisitionRun,
} from '../src/utils/externalAcquisitionPolling.ts'

test('polls only while at least one external acquisition run is active', () => {
  assert.equal(hasActiveExternalAcquisitionRun([]), false)
  assert.equal(hasActiveExternalAcquisitionRun([{ status: 'success' }, { status: 'failed' }]), false)
  assert.equal(hasActiveExternalAcquisitionRun([{ status: 'queued' }]), true)
  assert.equal(hasActiveExternalAcquisitionRun([{ status: 'running' }]), true)
})

test('external acquisition status polling uses a bounded interval', () => {
  assert.equal(EXTERNAL_RUN_POLL_INTERVAL_MS, 5000)
})
