import { useState } from "react";

/** Картинка из источника через прокси API. Если файл недоступен, блок просто не показывается. */
export function Thumb({ src, className = "", alt = "" }: { src: string; className?: string; alt?: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return <img src={`/api${src}`} alt={alt} loading="lazy" decoding="async" onError={() => setFailed(true)} className={`object-cover bg-sunken ${className}`} style={{ borderRadius: 3 }} />;
}
