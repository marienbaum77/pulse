export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

type Opts = { method?: string; body?: unknown; params?: Record<string, string | number | boolean | undefined | null> };

function messageFrom(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    const d = detail[0] as { loc?: (string | number)[]; msg?: string };
    const field = d.loc?.filter((x) => x !== "body").join(".");
    return field ? `${field}: ${d.msg}` : String(d.msg);
  }
  return `Ошибка ${status}`;
}

export async function api<T = unknown>(path: string, opts: Opts = {}): Promise<T> {
  const url = new URL("/api" + path, window.location.origin);
  for (const [k, v] of Object.entries(opts.params ?? {})) if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  const isForm = typeof FormData !== "undefined" && opts.body instanceof FormData;
  let res: Response;
  try {
    res = await fetch(url, {
      method: opts.method ?? "GET",
      credentials: "same-origin",
      headers: {
        "X-Requested-With": "pulse",
        ...(isForm || opts.body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: opts.body === undefined ? undefined : isForm ? (opts.body as FormData) : JSON.stringify(opts.body),
    });
  } catch {
    throw new ApiError("Нет связи с сервером", 0);
  }
  if (res.status === 401 && !path.startsWith("/auth/")) window.dispatchEvent(new Event("pulse:unauthorized"));
  if (!res.ok) {
    let detail: unknown;
    try { detail = (await res.json()).detail; } catch { /* тело не JSON */ }
    throw new ApiError(messageFrom(detail, res.status), res.status);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}
