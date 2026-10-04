// Управление Docker и Compose. Все команды выполняются в корне репозитория.
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

/** Docker Desktop ставит CLI в каталог, которого может не быть в PATH у процесса, запущенного из ярлыка. */
function augmentedPath() {
  const parts = (process.env.PATH || "").split(path.delimiter).filter(Boolean);
  if (process.platform === "win32") {
    const candidates = [
      "C:\\Program Files\\Docker\\Docker\\resources\\bin",
      path.join(process.env.ProgramFiles || "C:\\Program Files", "Docker", "Docker", "resources\\bin"),
    ];
    for (const dir of candidates) {
      if (fs.existsSync(dir) && !parts.includes(dir)) parts.push(dir);
    }
  }
  return parts.join(path.delimiter);
}

/** Путь к приложению Docker Desktop, если оно установлено. Нужен, чтобы предложить кнопку запуска. */
function dockerDesktopPath() {
  if (process.platform === "win32") {
    const candidates = [
      path.join(process.env.ProgramFiles || "C:\Program Files", "Docker", "Docker", "Docker Desktop.exe"),
      path.join(process.env["ProgramFiles(x86)"] || "C:\Program Files (x86)", "Docker", "Docker", "Docker Desktop.exe"),
    ];
    return candidates.find((p) => fs.existsSync(p)) || null;
  }
  if (process.platform === "darwin") {
    const app = "/Applications/Docker.app";
    return fs.existsSync(app) ? app : null;
  }
  return null;
}

/** Запускает docker с аргументами, отдавая вывод по мере поступления. Никогда не отклоняется — возвращает код. */
function run(args, { cwd, onOutput } = {}) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn("docker", args, { cwd, env: { ...process.env, PATH: augmentedPath() }, windowsHide: true });
    } catch (error) {
      resolve({ code: -1, out: "", error });
      return;
    }
    let out = "";
    const push = (chunk, stream) => {
      const text = chunk.toString();
      out += text;
      if (onOutput) onOutput(text, stream);
    };
    child.stdout.on("data", (d) => push(d, "stdout"));
    child.stderr.on("data", (d) => push(d, "stderr"));
    child.on("error", (error) => resolve({ code: -1, out, error }));
    child.on("close", (code) => resolve({ code, out }));
  });
}

async function cliInstalled() {
  const r = await run(["--version"]);
  return r.code === 0;
}

async function daemonRunning() {
  const r = await run(["info"]);
  return r.code === 0;
}

async function composeAvailable() {
  const r = await run(["compose", "version"]);
  return r.code === 0;
}

/** Запущен ли хотя бы один контейнер этого compose-проекта. */
async function stackRunning(cwd) {
  const r = await run(["compose", "ps", "-q"], { cwd });
  return r.code === 0 && r.out.trim().length > 0;
}

const compose = (args, opts) => run(["compose", ...args], opts);

async function up(cwd, onOutput) {
  return compose(["up", "-d", "--build"], { cwd, onOutput });
}

async function stop(cwd, onOutput) {
  return compose(["stop"], { cwd, onOutput });
}

async function pullEmbedModel(cwd, onOutput) {
  return compose(["exec", "-T", "ollama", "ollama", "pull", "bge-m3"], { cwd, onOutput });
}

module.exports = { run, cliInstalled, daemonRunning, composeAvailable, stackRunning, up, stop, pullEmbedModel, dockerDesktopPath };
