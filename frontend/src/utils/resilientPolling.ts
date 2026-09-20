export interface ResilientPollerOptions {
  intervalMs: number
  maxDelayMs?: number
  failureThreshold?: number
  cooldownMs?: number
  jitterRatio?: number
  random?: () => number
  setTimeout?: (callback: () => void, delay: number) => ReturnType<typeof globalThis.setTimeout>
  clearTimeout?: (timer: ReturnType<typeof globalThis.setTimeout>) => void
  onCircuitOpen?: () => void
  shouldRun?: () => boolean
}

export interface ResilientPoller {
  start(): void
  stop(): void
  trigger(): void
  readonly failures: number
  readonly circuitOpen: boolean
}

export function retryDelay(
  baseMs: number,
  failures: number,
  maxDelayMs: number,
  jitterRatio: number,
  random: () => number,
): number {
  const exponential = Math.min(maxDelayMs, baseMs * (2 ** Math.max(0, failures - 1)))
  const jitter = exponential * Math.max(0, Math.min(1, jitterRatio))
  return Math.max(0, Math.round(exponential - jitter + random() * jitter * 2))
}

/**
 * Run a background request without creating synchronized retry storms.
 * A rejected task is retried with bounded exponential backoff and jitter;
 * after the failure threshold the poller stays quiet for the cooldown window.
 */
export function createResilientPoller(
  task: () => void | Promise<void>,
  options: ResilientPollerOptions,
): ResilientPoller {
  const intervalMs = Math.max(1, options.intervalMs)
  const maxDelayMs = Math.max(intervalMs, options.maxDelayMs ?? intervalMs * 8)
  const failureThreshold = Math.max(1, options.failureThreshold ?? 4)
  const cooldownMs = Math.max(intervalMs, options.cooldownMs ?? intervalMs * 4)
  const jitterRatio = options.jitterRatio ?? 0.2
  const random = options.random ?? Math.random
  const setTimer = options.setTimeout ?? ((callback, delay) => globalThis.setTimeout(callback, delay))
  const clearTimer = options.clearTimeout ?? ((timer) => globalThis.clearTimeout(timer))
  let timer: ReturnType<typeof globalThis.setTimeout> | undefined
  let stopped = true
  let inFlight = false
  let failureCount = 0
  let open = false

  const schedule = (delay: number) => {
    if (stopped) return
    if (timer !== undefined) clearTimer(timer)
    timer = setTimer(() => {
      timer = undefined
      void run()
    }, delay)
  }

  const run = async () => {
    if (stopped || inFlight) return
    if (options.shouldRun && !options.shouldRun()) {
      schedule(intervalMs)
      return
    }
    inFlight = true
    try {
      await task()
      failureCount = 0
      open = false
      schedule(intervalMs)
    } catch {
      failureCount += 1
      if (failureCount >= failureThreshold) {
        open = true
        options.onCircuitOpen?.()
        schedule(cooldownMs)
      } else {
        schedule(retryDelay(intervalMs, failureCount, maxDelayMs, jitterRatio, random))
      }
    } finally {
      inFlight = false
    }
  }

  return {
    start() {
      if (!stopped) return
      stopped = false
      void run()
    },
    stop() {
      stopped = true
      if (timer !== undefined) clearTimer(timer)
      timer = undefined
    },
    trigger() {
      if (stopped || inFlight) return
      if (timer !== undefined) clearTimer(timer)
      timer = undefined
      void run()
    },
    get failures() { return failureCount },
    get circuitOpen() { return open },
  }
}
