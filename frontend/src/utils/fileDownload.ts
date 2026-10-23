import { resolveDesktopBridge } from '../desktop/bridge.ts'
import { resolveRuntimeApiUrl } from './apiEnvironment.ts'

interface DesktopFileBridge {
  saveFile: (filename: string, data: number[]) => Promise<boolean>
  beginSaveFile?: (filename: string) => Promise<string | null>
  writeSaveFileChunk?: (handle: string, data: number[], append: boolean) => Promise<void>
  finishSaveFile?: (handle: string) => Promise<void>
  cancelSaveFile?: (handle: string) => Promise<void>
}

const DESKTOP_FILE_CHUNK_BYTES = 256 * 1024

export async function downloadUrl(
  url: string,
  filename: string,
  onProgress?: (percent: number | null) => void,
): Promise<'saved' | 'started' | 'cancelled'> {
  const safeName = safeFileName(filename)
  const desktop = resolveDesktopBridge() as (DesktopFileBridge | null)
  if (!desktop?.beginSaveFile || !desktop.writeSaveFileChunk || !desktop.finishSaveFile || !desktop.cancelSaveFile) {
    const anchor = document.createElement('a')
    anchor.href = resolveRuntimeApiUrl(url)
    anchor.download = safeName
    anchor.rel = 'noopener'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    onProgress?.(null)
    return 'started'
  }

  const handle = await desktop.beginSaveFile(safeName)
  if (!handle) return 'cancelled'
  const controller = new AbortController()
  try {
    const response = await fetch(resolveRuntimeApiUrl(url), {
      credentials: 'include',
      headers: { 'X-User-Activity': '1' },
      signal: controller.signal,
    })
    if (!response.ok) {
      const body = await response.text()
      let detail = body
      try {
        const parsed = JSON.parse(body) as { detail?: unknown }
        if (typeof parsed.detail === 'string') detail = parsed.detail
      } catch {
        // Keep the response body when the server returned a non-JSON error.
      }
      throw new Error(detail || `下载失败（HTTP ${response.status}）`)
    }
    if (!response.body) throw new Error('服务器未提供可读取的下载流')

    const totalHeader = response.headers.get('content-length')
    const total = totalHeader ? Number(totalHeader) : 0
    const reader = response.body.getReader()
    let received = 0
    let firstChunk = true
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      for (let offset = 0; offset < value.length; offset += DESKTOP_FILE_CHUNK_BYTES) {
        const chunk = value.subarray(offset, Math.min(offset + DESKTOP_FILE_CHUNK_BYTES, value.length))
        await desktop.writeSaveFileChunk(handle, Array.from(chunk), !firstChunk)
        firstChunk = false
      }
      received += value.length
      onProgress?.(total > 0 ? Math.min(100, Math.round(received * 100 / total)) : null)
    }
    await desktop.finishSaveFile(handle)
    onProgress?.(100)
    return 'saved'
  } catch (error) {
    controller.abort()
    await desktop.cancelSaveFile(handle).catch(() => {})
    throw error
  }
}

function safeFileName(filename: string): string {
  const normalized = filename.trim().replace(/[\\/:*?"<>|]/g, '_')
  return normalized || '下载文件'
}

/** Save through the native shell when running in a Windows client. */
export async function downloadBlob(blob: Blob, filename: string): Promise<boolean> {
  const safeName = safeFileName(filename)
  const desktop = resolveDesktopBridge() as (DesktopFileBridge | null)
  if (desktop?.saveFile) {
    const bytes = new Uint8Array(await blob.arrayBuffer())
    if (desktop.beginSaveFile && desktop.writeSaveFileChunk && desktop.finishSaveFile && desktop.cancelSaveFile) {
      const handle = await desktop.beginSaveFile(safeName)
      if (!handle) return false
      try {
        if (bytes.length === 0) {
          await desktop.writeSaveFileChunk(handle, [], false)
          await desktop.finishSaveFile(handle)
          return true
        }
        for (let offset = 0; offset < bytes.length; offset += DESKTOP_FILE_CHUNK_BYTES) {
          const chunk = bytes.subarray(offset, Math.min(offset + DESKTOP_FILE_CHUNK_BYTES, bytes.length))
          await desktop.writeSaveFileChunk(handle, Array.from(chunk), offset > 0)
        }
        await desktop.finishSaveFile(handle)
        return true
      } catch (error) {
        await desktop.cancelSaveFile(handle).catch(() => {})
        throw error
      }
    }
    return desktop.saveFile(safeName, Array.from(bytes))
  }

  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = safeName
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  return true
}
