import { CloseOutlined, CopyOutlined, PhoneOutlined } from '@ant-design/icons'
import { Button } from 'antd'
import { useEffect } from 'react'

interface MobilePhonePickerProps {
  open: boolean
  phones: string[]
  onClose: () => void
  onDial: (phone: string) => void
  onCopy: (phone: string) => void
}

export default function MobilePhonePicker({
  open,
  phones,
  onClose,
  onDial,
  onCopy,
}: MobilePhonePickerProps) {
  useEffect(() => {
    if (!open) return undefined
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose, open])

  if (!open) return null

  return (
    <>
      <button
        type="button"
        className="mobile-phone-picker__backdrop"
        aria-label="关闭号码选择"
        onClick={onClose}
      />
      <section
        role="dialog"
        aria-modal="true"
        aria-label="选择电话号码"
        className="mobile-phone-picker"
      >
        <header className="mobile-phone-picker__header">
          <div>
            <h2>选择电话号码</h2>
            <p>该任务包含 {phones.length} 个号码</p>
          </div>
          <Button type="text" aria-label="关闭" icon={<CloseOutlined />} onClick={onClose} />
        </header>
        <div className="mobile-phone-picker__list">
          {phones.map(phone => (
            <div key={phone} className="mobile-phone-picker__item">
              <strong>{phone}</strong>
              <div className="mobile-phone-picker__actions">
                <Button icon={<CopyOutlined />} onClick={() => onCopy(phone)}>复制</Button>
                <Button type="primary" icon={<PhoneOutlined />} onClick={() => onDial(phone)}>拨打</Button>
              </div>
            </div>
          ))}
        </div>
      </section>
    </>
  )
}
