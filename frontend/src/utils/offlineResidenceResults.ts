import type { OfflineResidenceDiagnosticEvent, OfflineResidenceQueryResult } from './offlineResidenceClient'
import type { OfflineWorkbook } from './offlineResidenceXlsx'

export interface OfflineResidenceRowDetail {
  index: number
  sourceRow: number
  identity: string
  status: string
  category: 'result' | 'review' | 'invalid' | 'failure'
  reason: string
  action: string
}

const REASONS: Record<string, [string, string]> = {
  invalid_identity: ['身份证号未通过本地格式、日期或校验位检查；没有发起外部查询。', '返回原表核对该行身份证号，修正后重新查询。'],
  resident_response_contract_changed: ['常住人口预检索响应不符合当前已核验格式。', '检查居住证系统该人员的查询页面；如能正常查询，请提供脱敏后的响应字段结构供适配。'],
  resident_status_unconfirmed: ['常住人口预检索返回了资料，但尚无经核验的字段契约来确定是否为常口。', '在居住证系统人工核对该人员的常住人口状态；不要把此行当作未登记。'],
  registration_status_unconfirmed: ['流动人口登记资料已返回，但注销状态代码未能按当前枚举识别。', '在居住证系统人工核对该行的登记或注销状态。'],
  floating_response_contract_changed: ['流动人口查询响应结构发生变化。', '在居住证系统人工核对该行，并联系维护人员核对脱敏响应字段结构。'],
  floating_business_error: ['流动人口查询返回了非“没有查询到数据”的业务错误。', '在居住证系统人工核对该行，并检查账号查询权限。'],
  authentication_expired: ['居住证系统登录失效或认证被拒绝。', '检查当前客户端的社区账号、密码和 MAC 授权后重新查询。'],
  address_unavailable: ['查到登记资料，但居住证系统没有返回可用的登记地址字段。', '在居住证系统人工核对登记地址。'],
  config_incomplete: ['本地居住证查询配置不完整。', '在离线模式设置中补齐接口、账号、密码和 MAC 配置。'],
  disabled: ['本地居住证查询未开启。', '在离线模式设置中开启查询后重新运行。'],
  http_error: ['居住证系统请求返回 HTTP 错误。', '检查居住证系统连通性和账号权限，稍后重新查询。'],
  invalid_response: ['居住证系统响应无法解析。', '稍后重新查询；持续出现时联系维护人员核对接口。'],
  request_error: ['请求未取得明确业务结果。', '检查网络或居住证系统状态，稍后重新查询。'],
}

const STAGES: Record<string, string> = {
  captcha: '验证码', login: '登录', search_resident: '常住人口预检索', search_floating: '流动人口登记查询',
}

function diagnosticSuffix(events: OfflineResidenceDiagnosticEvent[] | undefined, code: string): string {
  const event = [...(events || [])].reverse().find(item => item.error_code === code)
    || (code === 'resident_status_unconfirmed'
      ? [...(events || [])].reverse().find(item => item.error_code === 'resident_precheck_match_unconfirmed')
      : undefined)
  if (!event) return ''
  const details = [STAGES[event.stage] || event.stage]
  if (event.http_status) details.push(`HTTP ${event.http_status}`)
  if (event.business_code && event.business_code !== 'missing') details.push(`业务码 ${event.business_code}`)
  return `（${details.join('，')}）`
}

export function makeOfflineResidenceRowDetail(book: OfflineWorkbook, index: number, result: OfflineResidenceQueryResult): OfflineResidenceRowDetail {
  const code = result.error || result.review || ''
  const [reason, action] = REASONS[code] || ['查询没有取得明确业务结果。', '在居住证系统人工核对该行；持续出现时联系维护人员。']
  return {
    index,
    sourceRow: book.source?.dataRows[index] ?? index + 2,
    identity: String(book.rows[index]?.[book.identityColumn] ?? ''),
    status: result.status,
    category: code === 'invalid_identity' ? 'invalid' : result.error ? 'failure' : result.review ? 'review' : 'result',
    reason: code ? `${reason}${diagnosticSuffix(result.diagnostics, code)}` : '已取得明确业务结果。',
    action: code ? action : '无需处理。',
  }
}
