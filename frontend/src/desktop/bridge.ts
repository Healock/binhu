export type ClientUpdatePhase =
  | 'idle'
  | 'checking'
  | 'available'
  | 'downloading'
  | 'ready'
  | 'applying'
  | 'error'

export interface ClientUpdateState {
  state: ClientUpdatePhase
  platform?: 'android' | 'win7' | 'win10' | 'windows'
  currentVersion: string
  currentVersionCode?: number
  availableVersion: string | null
  progress: number | null
  mandatory: boolean
  requiresInstallPermission?: boolean
  error: string | null
}

export type DesktopUpdatePhase = ClientUpdatePhase
export type DesktopUpdateState = ClientUpdateState

export interface DesktopUpgradeInfo {
  currentVersion: string
  upgradedFrom: string | null
  upgradeDetected?: boolean
}

export interface ResidenceProbeRequest {
  baseUrl: string
  username: string
  password: string
  mac: string
  timeoutSeconds: number
  communityCode?: string
}

export type ResidenceProbeStatus = 'allowed' | 'rejected' | 'network_error' | 'config_error'

export interface ResidenceProbeResult {
  status: ResidenceProbeStatus
  organizationCode?: string
  errorCode?: string
}

export interface ClientUpdateBridge {
  getUpdateStatus: () => Promise<ClientUpdateState>
  checkForUpdates: () => Promise<ClientUpdateState>
  downloadUpdate: () => Promise<ClientUpdateState>
  restartAndApply: () => Promise<ClientUpdateState>
  subscribeUpdateState: (listener: (state: ClientUpdateState) => void) => () => void
}

export interface DesktopBridge extends ClientUpdateBridge {
  saveFile: (filename: string, data: number[]) => Promise<boolean>
  openOffline: () => Promise<void>
  minimize: () => Promise<void>
  toggleMaximize: () => Promise<boolean>
  isMaximized: () => Promise<boolean>
  close: () => Promise<void>
  getUpgradeInfo: () => Promise<DesktopUpgradeInfo | null>
  acknowledgeUpgrade: () => Promise<DesktopUpgradeInfo | null>
  getLocalMac: () => Promise<string>
  setLocalMac: (mac: string) => Promise<string>
  probeResidenceLogin: (request: ResidenceProbeRequest) => Promise<ResidenceProbeResult>
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

export function isAndroidClientRuntime() {
  return typeof navigator !== 'undefined' && /Android/i.test(navigator.userAgent || '')
}

function resolveTauriUpdateBridge(eventName: string): ClientUpdateBridge | null {
  const desktopWindow = window as DesktopWindow
  const invoke = desktopWindow.__TAURI__?.core?.invoke
  const listen = desktopWindow.__TAURI__?.event?.listen
  if (!invoke || !listen) return null

  return {
    getUpdateStatus: () => invoke<ClientUpdateState>('get_update_status'),
    checkForUpdates: () => invoke<ClientUpdateState>('check_for_updates'),
    downloadUpdate: () => invoke<ClientUpdateState>('download_update'),
    restartAndApply: () => invoke<ClientUpdateState>('restart_and_apply'),
    subscribeUpdateState: listener => {
      let disposed = false
      let unlisten: (() => void) | undefined
      listen<ClientUpdateState>(eventName, event => listener(event.payload))
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

export function resolveClientUpdateBridge(): ClientUpdateBridge | null {
  const desktopWindow = window as DesktopWindow
  if (desktopWindow.binhuDesktop) return desktopWindow.binhuDesktop
  return resolveTauriUpdateBridge(isAndroidClientRuntime() ? 'client:update-state' : 'desktop:update-state')
}

export function resolveDesktopBridge(): DesktopBridge | null {
  if (typeof navigator !== 'undefined' && /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || '')) {
    return null
  }
  const desktopWindow = window as DesktopWindow
  if (desktopWindow.binhuDesktop) return desktopWindow.binhuDesktop
  const invoke = desktopWindow.__TAURI__?.core?.invoke
  const listen = desktopWindow.__TAURI__?.event?.listen
  if (!invoke || !listen) return null

  return {
    saveFile: (filename, data) => invoke<boolean>('save_file', { filename, data }),
    openOffline: () => invoke<void>('open_offline'),
    minimize: () => invoke<void>('window_minimize'),
    toggleMaximize: () => invoke<boolean>('window_toggle_maximize'),
    isMaximized: () => invoke<boolean>('window_is_maximized'),
    close: () => invoke<void>('window_close'),
    getUpdateStatus: () => invoke<DesktopUpdateState>('get_update_status'),
    getUpgradeInfo: () => invoke<DesktopUpgradeInfo>('get_upgrade_info'),
    acknowledgeUpgrade: () => invoke<DesktopUpgradeInfo>('acknowledge_upgrade'),
    getLocalMac: () => invoke<string>('get_local_mac'),
    setLocalMac: mac => invoke<string>('set_local_mac', { mac }),
    probeResidenceLogin: request => invoke<ResidenceProbeResult>('probe_residence_login', { request }),
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
