// Корень запуска. В режиме разработки — сам репозиторий; в собранном приложении исходники
// лежат только для чтения внутри пакета, поэтому при первом запуске (и после обновления)
// копируем их в записываемый каталог и запускаем docker compose уже там.
const fs = require("node:fs");
const path = require("node:path");

// Не копируем то, что пересоздаётся или не нужно для сборки образов.
// Проверяем только имя элемента: путь к собранному приложению может сам содержать «release»/«dist».
const SKIP_NAMES = new Set(["node_modules", "__pycache__", ".git"]);

function keep(src) {
  const base = path.basename(src);
  if (SKIP_NAMES.has(base) || base === ".env" || base.endsWith(".pyc")) return false;
  return true;
}

function copyRepo(from, to) {
  fs.cpSync(from, to, { recursive: true, force: true, errorOnExist: false, filter: keep });
}

/**
 * Возвращает рабочий каталог для docker compose.
 * dev: репозиторий как есть. packaged: userData/runtime, заполняемый из встроенных ресурсов.
 */
async function resolveRoot({ isPackaged, resourcesPath, userData, version, devRoot, onLog = () => {} }) {
  if (!isPackaged) return devRoot;

  const dest = path.join(userData, "runtime");
  const marker = path.join(dest, ".bundle-version");
  const bundled = path.join(resourcesPath, "repo");
  const current = fs.existsSync(marker) ? fs.readFileSync(marker, "utf8").trim() : "";

  // Исходники уже разложены для этой версии — ничего не трогаем (важно: не перезаписываем .env).
  if (current === version && fs.existsSync(path.join(dest, "docker-compose.yml"))) return dest;

  onLog(`Готовлю рабочую папку:\n${dest}\n`);
  fs.mkdirSync(dest, { recursive: true });
  copyRepo(bundled, dest);
  fs.writeFileSync(marker, version, "utf8");
  onLog("Готово.\n\n");
  return dest;
}

module.exports = { resolveRoot };
