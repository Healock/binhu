import type { MobileTaskAddressMatch } from '../api/client'

/** Rule scores rank candidates; they are not calibrated correctness probabilities. */
export function matchScoreText(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) return '暂无得分'
  return `${(value * 100).toFixed(1)} / 100`
}

function referenceConfidence(match: MobileTaskAddressMatch): string {
  if (match.status === 'conflict') return '需复核（归属冲突）'
  if (match.status === 'ambiguous') return '需复核（候选待确认）'
  if (match.status !== 'suggested') return '暂不评估'
  if (typeof match.score !== 'number' || !Number.isFinite(match.score) || match.score < 0 || match.score > 1) return '暂不评估'
  return match.score >= 0.9 ? '高（规则参考）' : match.score >= 0.75 ? '中（规则参考）' : '低（规则参考）'
}

export default function AddressMatchEvidence({ match }: { match?: MobileTaskAddressMatch | null }) {
  const manual = match?.status === 'confirmed' || match?.status === 'manual_unmatched'
  return <div className="address-match-evidence">
    <dl className="address-match-metrics" aria-label="匹配得分与置信度">
      <div><dt>匹配方式</dt><dd>{match?.method || '暂无'}</dd></div>
      {manual ? <div><dt>人工结论</dt><dd>{match?.status === 'confirmed' ? '已人工确认' : '无匹配小区'}</dd></div> : <>
        <div><dt>匹配得分</dt><dd>{matchScoreText(match?.score)}</dd></div>
        <div><dt>参考置信度</dt><dd>{match ? referenceConfidence(match) : '暂不评估'}</dd></div>
      </>}
    </dl>
    <p className="address-muted">{manual ? '人工结论优先，不换算为算法得分或正确率。下方候选为规则参考。' : '得分用于比较地址规则的匹配程度，不是匹配正确率。参考置信度仅按规则得分分档：90 分起为高，75 分起为中，其余为低；冲突或待复核时不评为高置信度。'}</p>
    {!!match?.candidates?.length && <ul className="address-candidate-evidence" aria-label="候选匹配得分">
      {match.candidates.map((candidate, index) => <li key={`${candidate.entry_id}-${index}`}>
        <div><strong>{String(candidate.name || '未命名候选')}</strong><span>{String(candidate.community_name || '社区未确定')}</span><span>得分 {matchScoreText(candidate.score)}</span></div>
        <span className="address-muted">{[candidate.method, candidate.reason].filter(Boolean).map(String).join(' · ') || '暂无匹配依据'}</span>
      </li>)}
    </ul>}
  </div>
}
