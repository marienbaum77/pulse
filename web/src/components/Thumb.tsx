import { useState } from "react";

/** Картинка из источника через прокси API в стилистике геометричной карточки Nothing OS. Если файл недоступен, блок не показывается. */
export function Thumb({ src, className = "", alt = "" }: { src: string; className?: string; alt?: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return (
    <img
      src={`/api${src}`}
      alt={alt}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
      className={`object-cover rounded-xl border border-rule/60 overflow-hidden bg-sunken/40 ${className}`}
    />
  );
}
