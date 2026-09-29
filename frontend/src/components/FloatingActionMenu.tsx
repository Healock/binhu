import { createContext, useContext, useState, type PropsWithChildren } from 'react'
import { AppstoreOutlined, CloseOutlined } from '@ant-design/icons'
import { FloatButton } from 'antd'

type FloatingActionMenuContextValue = { open: boolean }
const FloatingActionMenuContext = createContext<FloatingActionMenuContextValue>({ open: false })

export function useFloatingActionMenu(): FloatingActionMenuContextValue {
  return useContext(FloatingActionMenuContext)
}

export default function FloatingActionMenu({ children }: PropsWithChildren) {
  const [open, setOpen] = useState(false)
  return (
    <FloatingActionMenuContext.Provider value={{ open }}>
      <div className={`app-speed-dial ${open ? 'is-open' : ''}`}>
        {children}
        <FloatButton
          rootClassName="app-speed-dial__main"
          icon={open ? <CloseOutlined /> : <AppstoreOutlined />}
          tooltip={open ? '收起快捷功能' : '打开快捷功能'}
          aria-label={open ? '收起快捷功能' : '打开快捷功能'}
          onClick={() => setOpen(value => !value)}
        />
      </div>
    </FloatingActionMenuContext.Provider>
  )
}
