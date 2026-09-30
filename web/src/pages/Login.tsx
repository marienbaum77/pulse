import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/context";

export default function Login() {
  const { user, login } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (user) return <Navigate to={(loc.state as { from?: string } | null)?.from ?? "/"} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(email, password);
      nav("/", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось войти");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen grid lg:grid-cols-[1.1fr_1fr]">
      <section className="hidden lg:flex flex-col justify-between p-12 bg-brand text-brand-ink">
        <div className="flex items-center gap-3">
          <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden>
            <rect width="32" height="32" rx="5" fill="var(--brand-ink)" />
            <path d="M4 17h6l3-8 5 15 3-7h7" fill="none" stroke="var(--brand)" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="font-serif text-2xl font-semibold">Pulse</span>
        </div>
        <div className="max-w-[30ch]">
          <p className="font-serif text-[34px] leading-[1.15] font-semibold">Из потока новостей — в один проверенный пост.</p>
          <p className="mt-5 opacity-80 leading-relaxed">Собирает источники, находит сюжеты, оценивает их вес и готовит черновик со ссылками. Решение о публикации остаётся за редактором.</p>
        </div>
        <p className="text-sm opacity-60">Разворачивается на вашем сервере. Модели — локальные или любые OpenAI-совместимые.</p>
      </section>
      <section className="flex items-center justify-center p-6">
        <form onSubmit={submit} className="w-full max-w-[360px] fadein">
          <h1 className="text-[26px] font-semibold mb-1">Вход в редакцию</h1>
          <p className="text-muted mb-7">Введите почту и пароль, выданные администратором.</p>
          <label className="label" htmlFor="email">Почта</label>
          <input id="email" className="field mb-4" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
          <label className="label" htmlFor="password">Пароль</label>
          <input id="password" className="field mb-2" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          <p className="text-bad text-sm min-h-6" role="alert">{error}</p>
          <button className="btn btn-primary w-full h-10 mt-2" disabled={busy}>{busy ? "Входим…" : "Войти"}</button>
        </form>
      </section>
    </div>
  );
}
