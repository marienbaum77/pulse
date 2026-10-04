// Подбор свободного TCP-порта. Важно: проверяем не только привязку, но и активные подключения —
// на Windows порт, опубликованный Docker Desktop, виден через connect, хотя bind на 0.0.0.0 может «удаться».
const net = require("node:net");

/** true — на порту кто-то принимает соединения (или он занят). */
function isBusy(port, host = "127.0.0.1", timeoutMs = 700) {
  return new Promise((resolve) => {
    const socket = net.connect({ port, host });
    const finish = (busy) => {
      socket.destroy();
      resolve(busy);
    };
    socket.setTimeout(timeoutMs);
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(true)); // молчит — считаем занятым, чтобы не рисковать
    socket.once("error", () => finish(false)); // ECONNREFUSED — свободен
  });
}

function canBind(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen(port, "0.0.0.0", () => server.close(() => resolve(true)));
  });
}

/** Возвращает первый свободный порт начиная с preferred (до preferred+50). */
async function findFreePort(preferred = 8080) {
  for (let port = preferred; port < preferred + 50; port += 1) {
    if (!(await isBusy(port)) && (await canBind(port))) return port;
  }
  return preferred;
}

module.exports = { findFreePort, isBusy };
