import { useEffect, useState } from 'react'
import {
  BorderOutlined,
  CloseOutlined,
  FullscreenExitOutlined,
  MinusOutlined,
} from '@ant-design/icons'
import { resolveDesktopBridge } from '../desktop/bridge'
import DesktopUpdateControl from './DesktopUpdateControl'

export default function DesktopTitleBar() {
  const [maximized, setMaximized] = useState(false)
  const bridge = resolveDesktopBridge()

  const syncMaximizedState = (value: boolean) => {
    setMaximized(value)
    document.documentElement.dataset.windowMaximized = value ? 'true' : 'false'
  }

  useEffect(() => {
    if (!bridge) return
    const refresh = () => bridge.isMaximized().then(syncMaximizedState).catch(() => {})
    refresh()
    window.addEventListener('resize', refresh)
    return () => {
      window.removeEventListener('resize', refresh)
      delete document.documentElement.dataset.windowMaximized
    }
  }, [])

  if (!bridge) return null

  const toggleMaximize = () => {
    bridge.toggleMaximize().then(syncMaximizedState).catch(() => {})
  }

  return (
    <header className="desktop-titlebar" data-tauri-drag-region onDoubleClick={toggleMaximize}>
      <div className="desktop-titlebar__brand" data-tauri-drag-region>
        <span className="desktop-titlebar__mark" aria-hidden="true">滨</span>
        <span className="desktop-titlebar__title" data-tauri-drag-region>
          滨湖公安智慧平台
        </span>
      </div>
      <div className="desktop-titlebar__controls" onDoubleClick={event => event.stopPropagation()}>
        <DesktopUpdateControl />
        <button
          id="window-minimize-button"
          type="button"
          className="desktop-titlebar__button"
          title="最小化"
          aria-label="最小化窗口"
          onClick={() => bridge.minimize()}
        >
          <MinusOutlined />
        </button>
        <button
          id="window-maximize-button"
          type="button"
          className="desktop-titlebar__button"
          title={maximized ? '还原' : '最大化'}
          aria-label={maximized ? '还原窗口' : '最大化窗口'}
          onClick={toggleMaximize}
        >
          {maximized ? <FullscreenExitOutlined /> : <BorderOutlined />}
        </button>
        <button
          id="window-close-button"
          type="button"
          className="desktop-titlebar__button desktop-titlebar__button--close"
          title="关闭"
          aria-label="关闭窗口"
          onClick={() => bridge.close()}
        >
          <CloseOutlined />
        </button>
      </div>
    </header>
  )
}
