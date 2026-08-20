export type ClientDeviceType = 'desktop' | 'mobile'

const DEVICE_ID_KEY = 'binhu_device_id'
let fallbackDeviceId: string | null = null

function createDeviceId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `device-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export function getDeviceId(): string {
  try {
    const existing = window.localStorage.getItem(DEVICE_ID_KEY)
    if (existing && /^[A-Za-z0-9._:-]{16,128}$/.test(existing)) return existing
    const generated = fallbackDeviceId || createDeviceId()
    fallbackDeviceId = generated
    window.localStorage.setItem(DEVICE_ID_KEY, generated)
    return generated
  } catch {
    if (!fallbackDeviceId) fallbackDeviceId = createDeviceId()
    return fallbackDeviceId
  }
}

export function detectClientDeviceType(): ClientDeviceType {
  if (typeof navigator === 'undefined') return 'desktop'
  const ua = navigator.userAgent || ''
  if (/Android|iPhone|iPad|iPod|Mobile|Windows Phone/i.test(ua)) return 'mobile'
  if (typeof window !== 'undefined' && window.matchMedia?.('(max-width: 767px)').matches) {
    return 'mobile'
  }
  return 'desktop'
}

export function getClientDeviceHeaders(): Record<string, string> {
  return {
    'X-Binhu-Client-Platform': detectClientDeviceType(),
    'X-Binhu-Device-Id': getDeviceId(),
  }
}
