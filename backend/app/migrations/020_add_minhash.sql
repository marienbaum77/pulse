-- MinHash-сигнатура (64 элемента uint32, 256 байт) для O(1) поиска дубликатов на Stage 1
ALTER TABLE items ADD COLUMN IF NOT EXISTS minhash bytea;
