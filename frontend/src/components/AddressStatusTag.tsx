import { Tag } from 'antd'
import { useNavigate } from 'react-router-dom'
import { useRef, useState } from 'react'
import { ADDRESS_STATES, addressAnnotationUrl } from '../utils/addressAnnotation'
import { confirmPendingNavigation } from '../utils/navigationGuard'
export default function AddressStatusTag({ task, beforeOpen, onOpen }: {
  task: { parser_type: string; row_key: string; address_match?: { status?: string } | null }
  beforeOpen?: () => Promise<boolean> | boolean
  onOpen?: () => void
}) {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const label = ADDRESS_STATES[task.address_match?.status || 'unmatched'] || ADDRESS_STATES.unmatched
  return <a className="address-status-link" href={addressAnnotationUrl(task)} aria-label={`${label.text}，前往确认地址核对此任务`} aria-busy={busy}
    onClick={async event => {
      event.preventDefault(); event.stopPropagation()
      if (busyRef.current) return
      busyRef.current = true; setBusy(true)
      try {
        if (beforeOpen ? !await beforeOpen() : !confirmPendingNavigation()) return
        if (onOpen) onOpen()
        else navigate(addressAnnotationUrl(task), { state: { fromTask: true } })
      } finally { busyRef.current = false; setBusy(false) }
    }}><Tag color={label.color}>{busy ? '正在保存…' : label.text}</Tag></a>
}
