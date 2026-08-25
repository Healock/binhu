import { openUrl } from '@tauri-apps/plugin-opener'

const NATIVE_MOBILE = import.meta.env?.VITE_NATIVE_MOBILE === 'true'

export function normalizePhoneForDialer(phone: string): string {
  return phone.trim().replace(/[^+\d]/g, '')
}

export async function openNativePhoneDialer(phone: string): Promise<boolean> {
  if (!NATIVE_MOBILE) return false

  const normalized = normalizePhoneForDialer(phone)
  if (!normalized || !/^\+?\d+$/.test(normalized)) {
    throw new Error('电话号码格式无效')
  }

  await openUrl(`tel:${normalized}`)
  return true
}
