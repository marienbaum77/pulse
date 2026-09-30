// Тема применяется до отрисовки, чтобы не было вспышки светлой темы. Вынесено в файл ради строгой CSP (без inline-скриптов).
try {
  var t = localStorage.getItem("pulse.theme");
  if (t === "dark" || (!t && matchMedia("(prefers-color-scheme: dark)").matches)) document.documentElement.classList.add("dark");
} catch (e) {}
