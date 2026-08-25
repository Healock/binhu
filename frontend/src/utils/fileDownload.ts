import { resolveDesktopBridge } from '../desktop/bridge.ts'

interface DesktopFileBridge {
  saveFile: (filename: string, data: number[]) => Promise<void>
}

function safeFileName(filename: string): string {
  const normalized = filename.trim().replace(/[\\/:*?"<>|]/g, '_')
  return normalized || '下载文件'
}

/** Save through the native shell when running in a Windows client. */
export async function downloadBlob(blob: Blob, filename: string): Promise<void> {
  const safeName = safeFileName(filename)
  const desktop = resolveDesktopBridge() as (DesktopFileBridge | null)
  if (desktop?.saveFile) {
    const bytes = new Uint8Array(await blob.arrayBuffer())
    await desktop.saveFile(safeName, Array.from(bytes))
    return
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
}
