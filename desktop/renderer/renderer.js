// Отрисовка окна-лаунчера. Состояние определяет, какой экран показать:
// нет Docker → Docker не запущен → первый запуск → готов к запуску → работает.
const app = document.getElementById("app");
const foot = document.getElementById("foot");
const api = window.pulse;

const state = {
  info: null,
  busy: false,
  log: [],
  generatedPassword: null,
};

const escapeHtml = (value) =>
  String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function setFoot(text, tone = "") {
  foot.className = tone ? `foot ${tone}` : "foot";
  foot.textContent = text || "";
}

function appendLog(text) {
  state.log.push(text);
  const el = document.getElementById("log");
  if (el) {
    el.textContent += text;
    el.scrollTop = el.scrollHeight;
  }
}

async function refresh() {
  state.info = await api.detect();
  render();
}

function render() {
  if (!api) {
    app.innerHTML = `<section class="card"><h1>Ошибка загрузки</h1><p class="muted">Не удалось подключиться к процессу приложения.</p></section>`;
    return;
  }
  if (state.busy) return viewProgress();
  const info = state.info;
  if (!info) {
    app.innerHTML = `<section class="card"><p class="muted">Проверяю окружение…</p></section>`;
    return;
  }
  if (!info.dockerInstalled) return viewNoDocker();
  if (!info.dockerRunning) return viewDockerStopped();
  if (!info.envExists) return viewConfigure();
  if (!info.stackRunning) return viewStart();
  return viewRunning();
}

function viewNoDocker() {
  app.innerHTML = `
    <section class="card">
      <h1>Нужен Docker Desktop</h1>
      <p>Pulse работает в контейнерах Docker. Один раз установите <b>Docker Desktop</b> — дальше это окно запустит всё само.</p>
      <ol class="steps">
        <li>Нажмите «Скачать Docker Desktop».</li>
        <li>Откройте скачанный файл и установите, оставив галочку <b>Use WSL 2</b>.</li>
        <li>Если система попросит — перезагрузите компьютер.</li>
        <li>Запустите Docker Desktop и дождитесь статуса <b>Engine running</b>.</li>
        <li>Вернитесь в это окно и нажмите «Проверить снова».</li>
      </ol>
      <div class="row">
        <button class="btn primary" data-action="download-docker">Скачать Docker Desktop</button>
        <button class="btn" data-action="refresh">Проверить снова</button>
      </div>
    </section>`;
  setFoot("Страница загрузки откроется в браузере.");
}

function viewDockerStopped() {
  const canLaunch = !!state.info.dockerDesktopPath;
  app.innerHTML = `
    <section class="card">
      <h1>Docker установлен, но не запущен</h1>
      <p>Запустите Docker Desktop и дождитесь статуса <b>Engine running</b>.</p>
      <div class="row">
        ${canLaunch ? `<button class="btn primary" data-action="launch-docker">Запустить Docker Desktop</button>` : ""}
        <button class="btn" data-action="refresh">Проверить снова</button>
      </div>
    </section>`;
  setFoot(canLaunch ? "Первый запуск Docker Desktop занимает до минуты." : "Откройте Docker Desktop из меню «Пуск» и нажмите «Проверить снова».");
}

function viewConfigure() {
  app.innerHTML = `
    <section class="card">
      <h1>Первый запуск</h1>
      <p>Задайте данные администратора. Пароль базы и секрет сессий приложение сгенерирует само; ключ модели и токен бота можно добавить позже в интерфейсе.</p>
      <label class="fld"><span>Email администратора</span><input id="email" type="email" value="admin@example.com" /></label>
      <label class="fld"><span>Пароль администратора</span><input id="password" type="text" placeholder="оставьте пустым — сгенерируем" /></label>
      <label class="fld"><span>API-ключ чат-модели (необязательно)</span><input id="llm" type="text" placeholder="без ключа — упрощённая генерация" /></label>
      <label class="fld"><span>Токен Telegram-бота (необязательно)</span><input id="tg" type="text" /></label>
      <div class="row">
        <button class="btn primary" data-action="save-start">Сохранить и запустить</button>
      </div>
    </section>`;
  setFoot("Файл .env создаётся один раз и не перезаписывается.");
}

function viewStart() {
  app.innerHTML = `
    <section class="card">
      <h1>Готово к запуску</h1>
      <p>Docker работает. Нажмите кнопку — приложение соберёт и запустит Pulse, затем откроет интерфейс.</p>
      <p class="muted">Первый запуск занимает несколько минут: собираются образы и загружается модель эмбеддингов (~1.2 ГБ).</p>
      <div class="row">
        <button class="btn primary" data-action="start">Запустить Pulse</button>
        <button class="btn" data-action="refresh">Проверить снова</button>
      </div>
    </section>`;
  setFoot(`Адрес интерфейса: ${state.info.url}`);
}

function viewRunning() {
  app.innerHTML = `
    <section class="card">
      <h1>Pulse работает</h1>
      <p>Интерфейс доступен по адресу <span class="mono">${escapeHtml(state.info.url)}</span>.</p>
      ${state.generatedPassword ? `<div class="note"><b>Пароль администратора:</b> <span class="mono">${escapeHtml(state.generatedPassword)}</span> — запишите его.</div>` : ""}
      <div class="row">
        <button class="btn primary" data-action="open">Открыть интерфейс</button>
        <button class="btn" data-action="stop">Остановить</button>
        <button class="btn" data-action="refresh">Обновить</button>
      </div>
    </section>`;
  setFoot("Pulse работает, пока запущены это окно и Docker Desktop.");
}

function viewProgress() {
  app.innerHTML = `
    <section class="card">
      <h1>Запускаю Pulse…</h1>
      <p class="muted">Не закрывайте окно. Первый запуск занимает несколько минут.</p>
      <pre class="log" id="log"></pre>
    </section>`;
  setFoot("");
  const logEl = document.getElementById("log");
  logEl.textContent = state.log.join("");
  logEl.scrollTop = logEl.scrollHeight;
}

async function saveAndStart() {
  const result = await api.configure({
    adminEmail: document.getElementById("email").value,
    adminPassword: document.getElementById("password").value,
    llmApiKey: document.getElementById("llm").value,
    telegramToken: document.getElementById("tg").value,
  });
  if (result.created && result.adminPassword) state.generatedPassword = result.adminPassword;
  state.info = await api.detect();
  await start();
}

async function start() {
  state.busy = true;
  state.log = [];
  render();
  const result = await api.start();
  state.busy = false;
  state.info = await api.detect();
  render();
  if (!result.ok) setFoot(`Ошибка: ${result.error}`, "bad");
}

async function stop() {
  setFoot("Останавливаю…");
  await api.stop();
  await refresh();
}

document.addEventListener("click", async (event) => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const action = el.dataset.action;
  try {
    if (action === "download-docker") await api.openDockerDownload();
    else if (action === "refresh") await refresh();
    else if (action === "launch-docker") { await api.startDockerDesktop(); await refresh(); }
    else if (action === "save-start") await saveAndStart();
    else if (action === "start") await start();
    else if (action === "open") await api.open(state.info.url);
    else if (action === "stop") await stop();
  } catch (error) {
    setFoot(`Ошибка: ${error.message}`, "bad");
  }
});

if (api) api.onLog((payload) => appendLog(payload.line));
refresh();
