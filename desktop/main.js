// Главный процесс Electron: окно-лаунчер, IPC и управление стеком.
// Обёртка ничего не меняет в приложении — она лишь запускает docker compose в корне репозитория.
const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("node:path");

const docker = require("./lib/docker");
const envlib = require("./lib/env");
const health = require("./lib/health");
const netlib = require("./lib/net");
const runtime = require("./lib/runtime");

const DOCKER_DOWNLOAD_URL = "https://www.docker.com/products/docker-desktop/";

let launcher = null; // окно-лаунчер
let appWindow = null; // окно с самим интерфейсом Pulse
let busy = false;
let rootPromise = null;

/** Рабочий каталог со стеком: репозиторий (dev) или userData/runtime (собранное приложение). */
function root() {
  if (!rootPromise) {
    rootPromise = runtime.resolveRoot({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      userData: app.getPath("userData"),
      version: app.getVersion(),
      devRoot: envlib.repoRoot(),
      onLog: sendLog,
    });
  }
  return rootPromise;
}

function sendLog(line) {
  if (launcher && !launcher.isDestroyed()) launcher.webContents.send("pulse:log", { line });
}

function createLauncher() {
  launcher = new BrowserWindow({
    width: 760,
    height: 680,
    minWidth: 560,
    minHeight: 540,
    title: "Pulse",
    backgroundColor: "#f6f4ef",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  launcher.setMenuBarVisibility(false);
  launcher.loadFile(path.join(__dirname, "renderer", "index.html"));
}

/** Текущее состояние окружения: что установлено, что запущено, готов ли интерфейс. */
async function detect() {
  const ROOT = await root();
  const dockerInstalled = await docker.cliInstalled();
  const dockerRunning = dockerInstalled ? await docker.daemonRunning() : false;
  const composeAvailable = dockerRunning ? await docker.composeAvailable() : false;
  const envExists = envlib.envExists(ROOT);

  let stackRunning = false;
  let uiReady = false;
  if (dockerRunning && composeAvailable && envExists) {
    stackRunning = await docker.stackRunning(ROOT);
    if (stackRunning) uiReady = await health.probe(envlib.url(ROOT));
  }

  return {
    platform: process.platform,
    repoRoot: ROOT,
    dockerInstalled,
    dockerRunning,
    composeAvailable,
    dockerDesktopPath: docker.dockerDesktopPath(),
    envExists,
    stackRunning,
    uiReady,
    url: envlib.url(ROOT),
    port: envlib.port(ROOT),
  };
}

ipcMain.handle("pulse:detect", () => detect());

ipcMain.handle("pulse:configure", async (_event, config) => {
  const ROOT = await root();
  const result = envlib.ensureEnv(config || {}, ROOT);
  // Свежему .env сразу подбираем свободный порт: 8080 может быть занят другим стеком.
  if (result.created) envlib.setPort(await netlib.findFreePort(envlib.port(ROOT)), ROOT);
  return result;
});

ipcMain.handle("pulse:start", async () => {
  const ROOT = await root();
  if (busy) return { ok: false, error: "Запуск уже идёт" };
  busy = true;
  try {
    if (!envlib.envExists(ROOT)) return { ok: false, error: "Сначала заполните форму первого запуска" };

    // Если наш стек ещё не поднят, а порт занят (например, другой установкой Pulse), берём свободный.
    if (!(await docker.stackRunning(ROOT))) {
      const current = envlib.port(ROOT);
      const free = await netlib.findFreePort(current);
      if (free !== current) {
        envlib.setPort(free, ROOT);
        sendLog(`Порт ${current} занят — использую ${free}.\n\n`);
      }
    }

    sendLog("Собираю и запускаю контейнеры…\n");
    const up = await docker.up(ROOT, (text) => sendLog(text));
    if (up.code !== 0) return { ok: false, error: `docker compose up завершился с кодом ${up.code}. Смотрите вывод выше.` };

    sendLog("\nЗагружаю модель эмбеддингов bge-m3 (один раз, ~1.2 ГБ)…\n");
    const pull = await docker.pullEmbedModel(ROOT, (text) => sendLog(text));
    if (pull.code !== 0) sendLog("\nМодель не загрузилась — можно повторить позже. Продолжаю.\n");

    sendLog("\nЖду готовности интерфейса…\n");
    const ready = await health.waitForHttp(envlib.url(ROOT), { onTick: (n) => sendLog(`  проверка ${n}…\n`) });
    if (!ready) return { ok: false, error: "Интерфейс не отвечает. Проверьте журналы: docker compose logs --tail=100 api web" };

    sendLog("\nГотово.\n");
    return { ok: true, url: envlib.url(ROOT) };
  } catch (error) {
    return { ok: false, error: String(error && error.message ? error.message : error) };
  } finally {
    busy = false;
  }
});

ipcMain.handle("pulse:stop", async () => {
  const ROOT = await root();
  const result = await docker.stop(ROOT, (text) => sendLog(text));
  return { ok: result.code === 0 };
});

ipcMain.handle("pulse:open", async (_event, url) => {
  const ROOT = await root();
  const target = url || envlib.url(ROOT);
  if (appWindow && !appWindow.isDestroyed()) {
    appWindow.focus();
    appWindow.loadURL(target);
    return { ok: true };
  }
  appWindow = new BrowserWindow({
    width: 1240,
    height: 840,
    title: "Pulse",
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  appWindow.setMenuBarVisibility(false);
  appWindow.loadURL(target);
  appWindow.on("closed", () => { appWindow = null; });
  return { ok: true };
});

ipcMain.handle("pulse:openDockerDownload", () => shell.openExternal(DOCKER_DOWNLOAD_URL));

ipcMain.handle("pulse:startDockerDesktop", async () => {
  const desktopPath = docker.dockerDesktopPath();
  if (!desktopPath) return { ok: false, error: "Docker Desktop.exe не найден" };
  const error = await shell.openPath(desktopPath);
  return { ok: !error, error: error || undefined };
});

app.whenReady().then(createLauncher);
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createLauncher(); });
