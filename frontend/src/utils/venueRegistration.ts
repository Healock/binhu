export type VenueErrorPayload = { detail?: unknown; message?: unknown } | null

export async function readVenueErrorPayload(response: Response): Promise<VenueErrorPayload> {
  const contentType = response.headers.get('content-type')?.toLowerCase() || ''
  const body = await response.text()
  if (!contentType.includes('application/json') && !contentType.includes('+json')) return null
  try {
    const parsed: unknown = JSON.parse(body)
    return parsed && typeof parsed === 'object' ? parsed as VenueErrorPayload : null
  } catch {
    return null
  }
}

export function venueRegistrationErrorMessage(status: number, payload: VenueErrorPayload): string {
  const detail = payload && (typeof payload.detail === 'string' ? payload.detail : payload.message)
  if (typeof detail === 'string' && detail.trim()) return detail
  if (status === 413) return '照片或上传请求超过网关限制，请压缩照片后重试'
  if (status === 404) return '登记接口不存在或二维码入口与当前服务版本不一致，请重新扫码或联系管理员'
  if ([502, 503, 504].includes(status)) return '场所登记服务暂时不可用，请稍后重试'
  return '服务器返回了无法识别的错误页面，请稍后重试'
}
