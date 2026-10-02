# Фильтр дампа pg_dump: заменяет на NULL векторные столбцы (эмбеддинги, центроиды).
# Они занимают почти весь дамп (~95%), а восстанавливаются из текста материалов автоматически (см. process.restore_vectors).
BEGIN {
  FS = OFS = "\t"
  BS = sprintf("%c", 92)
  END_MARK = BS "."
  NULL_MARK = BS "N"
  split("embedding centroid published_centroid topic_embedding", names, " ")
  for (i in names) vec[names[i]] = 1
}
in_copy == 0 && /^COPY / {
  line = $0
  sub(/^COPY [^ ]+ \(/, "", line)
  sub(/\) FROM stdin;.*$/, "", line)
  n = split(line, cols, ", ")
  any = 0
  delete skip
  for (i = 1; i <= n; i++) if (cols[i] in vec) { skip[i] = 1; any = 1 }
  in_copy = 1
  print
  next
}
in_copy == 1 {
  if ($0 == END_MARK) { in_copy = 0; print; next }
  if (any) for (i in skip) $i = NULL_MARK
  print
  next
}
{ print }
