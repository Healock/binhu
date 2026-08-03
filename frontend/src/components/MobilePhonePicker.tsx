import { Button, type ButtonProps } from 'antd'
import type { ReactNode } from 'react'

interface MobilePhonePickerProps {
  phones: string[]
  mode: 'dial' | 'copy'
  label: ReactNode
  className?: string
  buttonProps?: Pick<ButtonProps, 'className' | 'ghost' | 'icon' | 'type'>
  onSelect: (phone: string) => void
}

export default function MobilePhonePicker({
  phones,
  mode,
  label,
  className = '',
  buttonProps,
  onSelect,
}: MobilePhonePickerProps) {
  if (phones.length === 0) return null

  if (phones.length === 1) {
    return (
      <Button
        {...buttonProps}
        onClick={event => {
          event.stopPropagation()
          onSelect(phones[0])
        }}
      >{label}</Button>
    )
  }

  return (
    <span className={`mobile-phone-native-select ${className}`.trim()} onClick={event => event.stopPropagation()}>
      <Button {...buttonProps} aria-hidden="true" tabIndex={-1}>{label}</Button>
      <select
        className="mobile-phone-native-select__control"
        defaultValue=""
        aria-label={mode === 'copy' ? '选择要复制的电话号码' : '选择要拨打的电话号码'}
        onChange={event => {
          const phone = event.currentTarget.value
          event.currentTarget.value = ''
          if (phone) onSelect(phone)
        }}
      >
        <option value="" disabled>{mode === 'copy' ? '选择要复制的号码' : '选择要拨打的号码'}</option>
        {phones.map(phone => <option key={phone} value={phone}>{phone}</option>)}
      </select>
    </span>
  )
}
