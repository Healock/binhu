const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('binhuDesktop', {
  target: 'win7',
  saveFile: (filename, data) => ipcRenderer.invoke('desktop:save-file', { filename, data }),
  appVersion: '0.27.3',
  getConfig: () => ipcRenderer.invoke('desktop:get-config'),
  openOnline: () => ipcRenderer.invoke('desktop:open-online'),
  openOffline: () => ipcRenderer.invoke('desktop:open-offline'),
  minimize: () => ipcRenderer.invoke('desktop:window-minimize'),
  toggleMaximize: () => ipcRenderer.invoke('desktop:window-toggle-maximize'),
  isMaximized: () => ipcRenderer.invoke('desktop:window-is-maximized'),
  close: () => ipcRenderer.invoke('desktop:window-close'),
  getUpdateStatus: () => ipcRenderer.invoke('desktop:get-update-status'),
  getUpgradeInfo: () => ipcRenderer.invoke('desktop:get-upgrade-info'),
  acknowledgeUpgrade: () => ipcRenderer.invoke('desktop:acknowledge-upgrade'),
  checkForUpdates: () => ipcRenderer.invoke('desktop:check-for-updates'),
  downloadUpdate: () => ipcRenderer.invoke('desktop:download-update'),
  restartAndApply: () => ipcRenderer.invoke('desktop:restart-and-apply'),
  subscribeUpdateState: (listener) => {
    const handler = (_event, state) => listener(state)
    ipcRenderer.on('desktop:update-state', handler)
    return () => ipcRenderer.removeListener('desktop:update-state', handler)
  },
})
