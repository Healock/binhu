import { resolveRuntimeApiUrl } from './apiEnvironment.ts'

export interface QueryEdit {
  column: string
  value: string
  expected_revision: number
  expected_row_hash: string
  explicit_text_edit?: boolean
}
export interface QueryEditResult {
  values: Record<string, string>
  row_key: string
  row_hash: string
  revision: number
  pending_sync: boolean
  message: string
  warnings?: string[]
  inspector_mismatch?: boolean
}
export type QueryConnectionState = 'connecting' | 'connected' | 'disconnected' | 'forbidden'

export function querySocketUrl(apiUrl: string, origin: string): string {
  const url = new URL(apiUrl, origin)
  if (!['https:', 'http:'].includes(url.protocol)) throw new Error('非法实时连接地址')
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

export function connectQueryRealtime(
  parserType: string,
  onVersion: (data: { data_version: string }) => void,
  onState: (state: QueryConnectionState) => void,
  options: { url?: string; socketFactory?: (url: string) => WebSocket } = {},
) {
  const url = options.url || querySocketUrl(
    resolveRuntimeApiUrl(`/api/query/live/${encodeURIComponent(parserType)}`), window.location.origin,
  )
  const factory = options.socketFactory || ((target: string) => new WebSocket(target))
  let socket: WebSocket | undefined
  let stopped = false
  let sequence = 0
  let retryDelay = 1000
  let retry: ReturnType<typeof setTimeout> | undefined
  const prefix = Math.random().toString(36).slice(2)
  const pending = new Map<string, {
    resolve: (result: QueryEditResult) => void
    reject: (error: Error) => void
    timer: ReturnType<typeof setTimeout>
  }>()
  function failPending() {
    for (const item of pending.values()) {
      clearTimeout(item.timer)
      item.reject(new Error('连接中断，保存结果待核对；请重新读取该行后再操作'))
    }
    pending.clear()
  }
  function connect() {
    if (stopped) return
    onState('connecting')
    const current = factory(url)
    socket = current
    current.onopen = () => { retryDelay = 1000; onState('connected') }
    current.onmessage = event => {
      if (stopped || socket !== current) return
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'version' && typeof data.data_version === 'string') {
          onVersion({ data_version: data.data_version })
          return
        }
        const item = pending.get(data.request_id)
        if (!item || !['saved', 'error'].includes(data.type)) return
        clearTimeout(item.timer)
        pending.delete(data.request_id)
        if (data.type === 'saved') item.resolve(data.result)
        else {
          const error = Object.assign(new Error(typeof data.detail === 'string' ? data.detail : '保存失败'), {
            response: { status: data.status, data: { detail: data.detail } },
          })
          item.reject(error)
        }
      } catch { /* Ignore malformed notifications; normal reads remain authoritative. */ }
    }
    current.onclose = event => {
      if (stopped || socket !== current) return
      failPending()
      onState(event.code === 1008 ? 'forbidden' : 'disconnected')
      if (event.code !== 1008) {
        retry = setTimeout(connect, retryDelay)
        retryDelay = Math.min(retryDelay * 2, 30000)
      }
    }
    current.onerror = () => { /* onclose owns reconnect and pending-write handling. */ }
  }
  connect()
  return {
    save(sourceId: number, payload: QueryEdit): Promise<QueryEditResult> {
      if (stopped || !socket || socket.readyState !== 1) {
        return Promise.reject(new Error('实时连接尚未就绪，请等待重连后再保存'))
      }
      const requestId = `${prefix}_${++sequence}`
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(requestId)
          reject(new Error('保存响应超时，结果待核对；请重新读取该行后再操作'))
        }, 30000)
        pending.set(requestId, { resolve, reject, timer })
        try { socket!.send(JSON.stringify({ type: 'edit', request_id: requestId, source_id: sourceId, ...payload })) }
        catch { failPending() }
      })
    },
    close() {
      stopped = true
      clearTimeout(retry)
      socket?.close()
      failPending()
    },
  }
}
