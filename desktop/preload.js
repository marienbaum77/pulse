// Мост между окном и главным процессом: окно не получает доступ к Node, только перечисленные вызовы.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pulse", {
  detect: () => ipcRenderer.invoke("pulse:detect"),
  configure: (config) => ipcRenderer.invoke("pulse:configure", config),
  start: () => ipcRenderer.invoke("pulse:start"),
  stop: () => ipcRenderer.invoke("pulse:stop"),
  open: (url) => ipcRenderer.invoke("pulse:open", url),
  openDockerDownload: () => ipcRenderer.invoke("pulse:openDockerDownload"),
  startDockerDesktop: () => ipcRenderer.invoke("pulse:startDockerDesktop"),
  onLog: (callback) => ipcRenderer.on("pulse:log", (_event, payload) => callback(payload)),
});
