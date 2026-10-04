// Работа с .env: чтение порта и разовый bootstrap файла из .env.example.
// Существующий .env никогда не перезаписывается: bootstrap выполняется ровно один раз,
// поэтому приложение можно переустанавливать и обновлять без потери настроек.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

/** Корень репозитория: обёртка лежит в desktop/lib, поэтому поднимаемся на два уровня. */
function repoRoot() {
  return path.resolve(__dirname, "..", "..");
}

function randHex(bytes) {
  return crypto.randomBytes(bytes).toString("hex");
}

/** Разбирает .env в объект, отбрасывая inline-комментарии после значения. */
function parseEnv(text) {
  const out = {};
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
    if (m) out[m[1]] = m[2].replace(/\s+#.*$/, "").trim();
  }
  return out;
}

function envExists(root = repoRoot()) {
  return fs.existsSync(path.join(root, ".env"));
}

function readEnv(root = repoRoot()) {
  const file = path.join(root, ".env");
  return fs.existsSync(file) ? parseEnv(fs.readFileSync(file, "utf8")) : {};
}

function port(root = repoRoot()) {
  const p = parseInt(readEnv(root).PULSE_PORT || "8080", 10);
  return Number.isFinite(p) && p > 0 ? p : 8080;
}

/** Переписывает PULSE_PORT в существующем .env, сохраняя остальные строки. */
function setPort(value, root = repoRoot()) {
  const file = path.join(root, ".env");
  if (!fs.existsSync(file)) return;
  const lines = fs.readFileSync(file, "utf8").split(/\r?\n/);
  const updated = lines.map((line) => (line.startsWith("PULSE_PORT=") ? `PULSE_PORT=${value}` : line));
  fs.writeFileSync(file, updated.join("\n"), "utf8");
}

function url(root = repoRoot()) {
  return `http://localhost:${port(root)}`;
}

/** Создаёт .env из образца, подставляя секреты и данные администратора. Возвращает созданный пароль. */
function ensureEnv({ adminEmail, adminPassword, llmApiKey, telegramToken } = {}, root = repoRoot()) {
  if (envExists(root)) return { created: false, adminPassword: null };
  const password = adminPassword && adminPassword.trim() ? adminPassword.trim() : randHex(8);
  const values = {
    POSTGRES_PASSWORD: randHex(16),
    SECRET_KEY: randHex(32),
    ADMIN_EMAIL: (adminEmail && adminEmail.trim()) || "admin@example.com",
    ADMIN_PASSWORD: password,
    LLM_API_KEY: (llmApiKey || "").trim(),
    TELEGRAM_BOT_TOKEN: (telegramToken || "").trim(),
  };
  if (!values.LLM_API_KEY) values.LLM_PROVIDER = "stub"; // без ключа — упрощённая генерация без чат-модели

  const example = fs.readFileSync(path.join(root, ".env.example"), "utf8");
  const lines = example.split(/\r?\n/).map((line) => {
    for (const [key, value] of Object.entries(values)) {
      if (line.startsWith(`${key}=`)) return `${key}=${value}`;
    }
    return line;
  });
  fs.writeFileSync(path.join(root, ".env"), lines.join("\n") + "\n", { encoding: "utf8", mode: 0o600 });
  return { created: true, adminPassword: password };
}

module.exports = { repoRoot, ensureEnv, readEnv, envExists, port, setPort, url, randHex };
