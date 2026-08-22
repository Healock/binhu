export type DesktopUpdatePhase =
  | 'idle'
  | 'checking'
  | 'available'
  | 'downloading'
  | 'ready'
  | 'applying'
  | 'error'

export interface DesktopUpdateState {
  state: DesktopUpdatePhase
  currentVersion: string
  availableVersion: string | null
  progress: number | null
  mandatory: boolean
  error: string | null
}

export interface DesktopBridge {
  openOffline: () => Promise<void>
  minimize: () => Promise<void>
  toggleMaximize: () => Promise<boolean>
  isMaximized: () => Promise<boolean>
  close: () => Promise<void>
  getUpdateStatus: () => Promise<DesktopUpdateState>
  checkForUpdates: () => Promise<DesktopUpdateState>
  downloadUpdate: () => Promise<DesktopUpdateState>
  restartAndApply: () => Promise<DesktopUpdateState>
  subscribeUpdateState: (listener: (state: DesktopUpdateState) => void) => () => void
}

interface TauriEvent<T> {
  payload: T
}

interface DesktopWindow extends Window {
  binhuDesktop?: DesktopBridge
  __TAURI__?: {
    core?: {
      invoke: <T>(command: string) => Promise<T>
    }
    event?: {
      listen: <T>(event: string, listener: (event: TauriEvent<T>) => void) => Promise<() => void>
    }
  }
}

export function resolveDesktopBridge(): DesktopBridge | null {
  const desktopWindow = window as DesktopWindow
  if (desktopWindow.binhuDesktop) return desktopWindow.binhuDesktop
  const invoke = desktopWindow.__TAURI__?.core?.invoke
  const listen = desktopWindow.__TAURI__?.event?.listen
  if (!invoke || !listen) return null

  return {
    openOffline: () => invoke<void>('open_offline'),
    minimize: () => invoke<void>('window_minimize'),
    toggleMaximize: () => invoke<boolean>('window_toggle_maximize'),
    isMaximized: () => invoke<boolean>('window_is_maximized'),
    close: () => invoke<void>('window_close'),
    getUpdateStatus: () => invoke<DesktopUpdateState>('get_update_status'),
    checkForUpdates: () => invoke<DesktopUpdateState>('check_for_updates'),
    downloadUpdate: () => invoke<DesktopUpdateState>('download_update'),
    restartAndApply: () => invoke<DesktopUpdateState>('restart_and_apply'),
    subscribeUpdateState: listener => {
      let disposed = false
      let unlisten: (() => void) | undefined
      listen<DesktopUpdateState>('desktop:update-state', event => listener(event.payload))
        .then(stop => {
          if (disposed) stop()
          else unlisten = stop
        })
        .catch(() => {})
      return () => {
        disposed = true
        unlisten?.()
      }
    },
  }
}
