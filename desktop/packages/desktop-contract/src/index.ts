export type DesktopTarget = 'win7' | 'win10-plus'

export type DesktopShellAction = 'online' | 'offline'

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

export interface DesktopConfig {
  schemaVersion: number
  appName: string
  appVersion: string
  onlineUrl: string
  offlineRoute: string
}

export interface DesktopBridge {
  target: DesktopTarget
  appVersion: string
  openOnline(): Promise<void>
  openOffline(): Promise<void>
  getConfig(): Promise<DesktopConfig>
  getUpdateStatus(): Promise<DesktopUpdateState>
  checkForUpdates(): Promise<DesktopUpdateState>
  downloadUpdate(): Promise<DesktopUpdateState>
  restartAndApply(): Promise<DesktopUpdateState>
  subscribeUpdateState(listener: (state: DesktopUpdateState) => void): () => void
}
