// Проверка готовности веб-интерфейса: опрос HTTP до первого ответа 200.
const http = require("node:http");

function probe(url, timeoutMs = 3000) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}

/** Ждёт ответа интерфейса. Возвращает true, если дождались, иначе false по таймауту. */
async function waitForHttp(url, { timeoutMs = 300000, intervalMs = 2000, onTick } = {}) {
  const deadline = Date.now() + timeoutMs;
  let attempt = 0;
  while (Date.now() < deadline) {
    if (await probe(url)) return true;
    attempt += 1;
    if (onTick) onTick(attempt);
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return false;
}

module.exports = { probe, waitForHttp };
