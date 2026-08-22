const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('binhuDesktop', {
  target: 'win7',
  appVersion: '0.25.15',
  getConfig: () => ipcRenderer.invoke('desktop:get-config'),
  openOnline: () => ipcRenderer.invoke('desktop:open-online'),
  openOffline: () => ipcRenderer.invoke('desktop:open-offline'),
  minimize: () => ipcRenderer.invoke('desktop:window-minimize'),
  toggleMaximize: () => ipcRenderer.invoke('desktop:window-toggle-maximize'),
  isMaximized: () => ipcRenderer.invoke('desktop:window-is-maximized'),
  close: () => ipcRenderer.invoke('desktop:window-close'),
  getUpdateStatus: () => ipcRenderer.invoke('desktop:get-update-status'),
  checkForUpdates: () => ipcRenderer.invoke('desktop:check-for-updates'),
  downloadUpdate: () => ipcRenderer.invoke('desktop:download-update'),
  restartAndApply: () => ipcRenderer.invoke('desktop:restart-and-apply'),
  subscribeUpdateState: (listener) => {
    const handler = (_event, state) => listener(state)
    ipcRenderer.on('desktop:update-state', handler)
    return () => ipcRenderer.removeListener('desktop:update-state', handler)
  },
})
