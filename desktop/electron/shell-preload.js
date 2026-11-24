'use strict';

/**
 * Minimal bridge for the shell's own pages (splash and error).
 *
 * It is attached only to those windows. The archive window has no preload at
 * all, so the product page cannot reach any system capability.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desktopShell', {
  getContext: () => ipcRenderer.invoke('shell:context'),
  retry: () => ipcRenderer.send('shell:retry'),
  quit: () => ipcRenderer.send('shell:quit'),
});
