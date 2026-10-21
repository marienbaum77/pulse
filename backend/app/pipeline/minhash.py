"""Лексическая дедупликация новостей методом MinHash и Locality-Sensitive Hashing (LSH).

Позволяет за O(1) отсекать синдицированные перепечатки (ТАСС, РИА Новости, пресс-релизы)
на первом этапе конвейера ДО ресурсоёмкого вычисления плотных нейросетевых эмбеддингов (bge-m3).
"""
import re
import zlib
from typing import Any

import numpy as np

NUM_PERMUTATIONS = 64  # размер сигнатуры MinHash
NUM_BANDS = 16         # число полос (bands) в LSH
ROWS_PER_BAND = 4      # число строк в полосе: 16 * 4 = 64
MERSENNE_PRIME = 2147483647  # 2^31 - 1 (Мерсенновское простое число)
DEFAULT_THRESHOLD = 0.82     # порог сходства Жаккара для признания перепечаткой
DEFAULT_SHINGLE_K = 4        # размер символьного шингла (4-граммы)

# Детерминированные коэффициенты линейных конгруэнтных хеш-функций: h_i(x) = (a_i * x + b_i) mod p
# Фиксированный seed обеспечивает воспроизводимость между запусками воркеров
_RNG = np.random.RandomState(42)
_HASH_A = _RNG.randint(1, MERSENNE_PRIME, size=(1, NUM_PERMUTATIONS), dtype=np.int64)
_HASH_B = _RNG.randint(0, MERSENNE_PRIME, size=(1, NUM_PERMUTATIONS), dtype=np.int64)


def normalize_text(text: str) -> str:
    """Нормализует текст для шинглинга: нижний регистр, очистка от пунктуации и лишних пробелов."""
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return " ".join(cleaned.split())


def shingle_text(text: str, k: int = DEFAULT_SHINGLE_K, mode: str = "char") -> set[int]:
    """Строит множество 32-битных хешей шинглов текста.

    Поддерживает:
      - mode='char': символьные k-граммы (по умолчанию k=4, оптимально для русского языка и опечаток);
      - mode='word': пословные n-граммы (k-словные шинглы).
    """
    norm = normalize_text(text)
    if not norm:
        return set()

    if mode == "word":
        words = norm.split()
        if len(words) < k:
            return {zlib.crc32(norm.encode("utf-8"))}
        return {
            zlib.crc32(" ".join(words[i : i + k]).encode("utf-8"))
            for i in range(len(words) - k + 1)
        }
    else:
        if len(norm) < k:
            return {zlib.crc32(norm.encode("utf-8"))}
        return {
            zlib.crc32(norm[i : i + k].encode("utf-8"))
            for i in range(len(norm) - k + 1)
        }


def compute_minhash(text: str, k: int = DEFAULT_SHINGLE_K, mode: str = "char") -> np.ndarray:
    """Вычисляет 64-элементную MinHash-сигнатуру (uint32) для входного текста."""
    shingles = shingle_text(text, k=k, mode=mode)
    if not shingles:
        return np.zeros(NUM_PERMUTATIONS, dtype=np.uint32)
    s = np.asarray(list(shingles), dtype=np.int64)[:, None]
    h = (s * _HASH_A + _HASH_B) % MERSENNE_PRIME
    return h.min(axis=0).astype(np.uint32)


def minhash_jaccard(sig_a: np.ndarray | None, sig_b: np.ndarray | None) -> float:
    """Несмещённая статистическая оценка сходства Жаккара: J(A, B) ≈ доля совпадающих компонент сигнатур."""
    if sig_a is None or sig_b is None:
        return 0.0
    if len(sig_a) != len(sig_b) or len(sig_a) == 0:
        return 0.0
    if not np.any(sig_a) or not np.any(sig_b):
        return 0.0
    return float(np.mean(sig_a == sig_b))


class MinHashLSHIndex:
    """Индекс Locality-Sensitive Hashing для суб-линейного поиска почти-дубликатов.

    Разбивает MinHash-сигнатуру длины m на b полос по r строк (b * r = m).
    Вероятность попадания документов с коэффициентом Жаккара s в один бакет:
        P(collision) = 1 - (1 - s^r)^b
    При b=16, r=4 и s=0.82 вероятность обнаружения > 99.99%.
    """

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        num_perm: int = NUM_PERMUTATIONS,
        num_bands: int = NUM_BANDS,
    ):
        self.threshold = threshold
        self.num_perm = num_perm
        self.num_bands = num_bands
        self.rows_per_band = num_perm // num_bands
        self.buckets: dict[tuple[int, bytes], list[int]] = {}
        self.signatures: dict[int, np.ndarray] = {}
        self.metadata: dict[int, dict[str, Any]] = {}

    def insert(self, item_id: int, sig: np.ndarray, meta: dict[str, Any] | None = None) -> None:
        """Регистрирует документ и его MinHash-сигнатуру в индексных бакетах LSH."""
        if not np.any(sig):
            return
        self.signatures[item_id] = sig
        if meta is not None:
            self.metadata[item_id] = meta

        for band in range(self.num_bands):
            start = band * self.rows_per_band
            end = start + self.rows_per_band
            band_key = (band, bytes(sig[start:end]))
            self.buckets.setdefault(band_key, []).append(item_id)

    def query(self, sig: np.ndarray, threshold: float | None = None) -> tuple[int | None, float]:
        """Ищет ближайший дубликат среди кандидатов, сколлидировавших хотя бы в одной полосе.

        Возвращает:
          (best_item_id, jaccard_similarity) при similarity >= threshold,
          иначе (None, 0.0).
        """
        if not np.any(sig):
            return None, 0.0

        th = self.threshold if threshold is None else threshold
        candidates: set[int] = set()

        for band in range(self.num_bands):
            start = band * self.rows_per_band
            end = start + self.rows_per_band
            band_key = (band, bytes(sig[start:end]))
            for cid in self.buckets.get(band_key, ()):
                candidates.add(cid)

        best_id: int | None = None
        best_sim: float = 0.0

        for cid in candidates:
            cand_sig = self.signatures[cid]
            sim = minhash_jaccard(sig, cand_sig)
            if sim >= th and sim > best_sim:
                best_sim = sim
                best_id = cid

        return best_id, best_sim

    def get_meta(self, item_id: int) -> dict[str, Any] | None:
        """Возвращает сохранённые метаданные документа по ID."""
        return self.metadata.get(item_id)

    def __len__(self) -> int:
        return len(self.signatures)
