import { InfoCircleOutlined } from '@ant-design/icons'
import { Tooltip } from 'antd'
import type { MobileTaskReviewFlow } from '../api/client'

interface Props {
  flow: MobileTaskReviewFlow
  showStateLabel?: boolean
  detailed?: boolean
}

export default function UnverifiableReviewNotice({ flow, showStateLabel = false, detailed = false }: Props) {
  const isExtension = ['initial_extension', 'deep_extension'].includes(flow.state)
  const isTerminal = ['final_unverifiable', 'source_exception'].includes(flow.state)
  const reminder = '已经能够核实时，请修改“核查结果”；只填写二次反馈不会结束无法核实流程。'
  const terminalMessage = flow.state === 'source_exception'
    ? '来源信息发生变化，自动流转已暂停，请由基础管控复核。'
    : '已形成最终无法核实，等待在当前业务中导出归档。'

  return (
    <div className={`mobile-task-review-notice ${isTerminal ? 'mobile-task-review-notice--terminal' : ''}`}>
      <div className="mobile-task-review-notice__meta">
        {showStateLabel && <span className="mobile-task-review-notice__stage">{flow.state_label}</span>}
        {flow.review_due_date && <span>复核截止：{flow.review_due_date}</span>}
        {isExtension && <span>本轮反馈：{flow.feedback_submitted ? '已记录' : '未记录'}</span>}
      </div>
      {isTerminal ? (
        <span className="mobile-task-review-notice__message">{terminalMessage}</span>
      ) : detailed ? (
        <span className="mobile-task-review-notice__message">{reminder}</span>
      ) : (
        <Tooltip title={reminder} placement="topLeft">
          <span className="mobile-task-review-notice__reminder">
            <InfoCircleOutlined aria-hidden />
            核实后请及时更新核查结果
          </span>
        </Tooltip>
      )}
    </div>
  )
}
