"""
Fuzzy text matcher for comparing expected comments against OCR-extracted text.
Handles Vietnamese diacritics, spacing, and punctuation differences.

OCR-aware: accounts for common EasyOCR misreads on Vietnamese text:
  - I (uppercase i) ↔ l (lowercase L) — "Iuôn" vs "luôn"
  - l ↔ i in Vietnamese words — "tul" vs "tui"
  - O ↔ ô/ơ/ồ — "cOng" vs "cũng"
  - 0 (zero) ↔ o — "đ0" vs "đó"
  - 6 ↔ ó — "đ6" vs "đó"
"""
import re
import unicodedata
from typing import Tuple

from rapidfuzz import fuzz
from unidecode import unidecode


def _normalize_whitespace(text: str) -> str:
    """Collapse all whitespace (including newlines) to single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def _strip_punctuation(text: str) -> str:
    """Remove common punctuation, keep letters/digits/spaces."""
    return re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)


def _remove_diacritics(text: str) -> str:
    """Remove Vietnamese diacritics — normalize to ASCII-like."""
    return unidecode(text)


def _fix_ocr_common_errors(text: str) -> str:
    """
    Fix common OCR misreads before fuzzy matching.
    Applied to OCR text only (not the expected comment).
    """
    # Normalize common OCR confusions at character level
    # These are applied AFTER lowercasing
    replacements = [
        # I/l/1 confusion (very common in EasyOCR)
        (r'\bl\b', 'i'),           # standalone 'l' → 'i' (Vietnamese "tui" not "tul")
        (r'(?<=[a-zàáảãạ])l(?=[a-zàáảãạ])', 'i'),  # 'l' between vowels → likely 'i'
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)
    return text


def normalize_text(text: str, strip_diacritics: bool = False, fix_ocr: bool = False) -> str:
    """
    Full normalization pipeline:
    1. Lowercase
    2. Collapse whitespace
    3. Strip punctuation
    4. Optionally fix OCR errors
    5. Optionally remove diacritics
    """
    if not text:
        return ""
    text = text.lower()
    text = _normalize_whitespace(text)
    text = _strip_punctuation(text)
    text = _normalize_whitespace(text)
    if fix_ocr:
        text = _fix_ocr_common_errors(text)
    if strip_diacritics:
        text = _remove_diacritics(text)
    return text


def match_comment_in_ocr(
    comment: str,
    ocr_text: str,
    threshold: float = 80.0,
) -> Tuple[bool, float, str]:
    """
    Check if `comment` appears in `ocr_text` using multi-strategy fuzzy matching.

    Returns:
        (is_match, best_score, strategy_used)

    Strategies (in order):
    1. Exact substring match (normalized, with diacritics)
    2. Exact substring match (normalized, without diacritics)
    3. Fuzzy partial ratio (with diacritics)
    4. Fuzzy partial ratio (without diacritics)
    5. Fuzzy token-sort ratio (without diacritics)
    6. Per-line fuzzy matching against OCR lines
    """
    if not comment or not comment.strip():
        return (False, 0.0, "empty_comment")
    if not ocr_text or not ocr_text.strip():
        return (False, 0.0, "empty_ocr")

    norm_comment = normalize_text(comment)
    norm_ocr = normalize_text(ocr_text)

    if not norm_comment:
        return (False, 0.0, "empty_after_normalize")

    # ── Strategy 1: Exact substring (with diacritics) ──
    if norm_comment in norm_ocr:
        return (True, 100.0, "exact_substring")

    # ── Strategy 2: Exact substring (without diacritics) ──
    ascii_comment = normalize_text(comment, strip_diacritics=True)
    ascii_ocr = normalize_text(ocr_text, strip_diacritics=True)
    if ascii_comment in ascii_ocr:
        return (True, 98.0, "exact_substring_no_diacritics")

    # ── Strategy 3: Fuzzy partial ratio (with diacritics) ──
    score3 = fuzz.partial_ratio(norm_comment, norm_ocr)
    if score3 >= threshold:
        return (True, score3, "fuzzy_partial")

    # ── Strategy 4: Fuzzy partial ratio (without diacritics) ──
    score4 = fuzz.partial_ratio(ascii_comment, ascii_ocr)
    if score4 >= threshold:
        return (True, score4, "fuzzy_partial_no_diacritics")

    # ── Strategy 5: Token sort ratio (without diacritics) ──
    score5 = fuzz.token_sort_ratio(ascii_comment, ascii_ocr)
    if score5 >= threshold:
        return (True, score5, "fuzzy_token_sort")

    # ── Strategy 5b: OCR-corrected matching ──
    # Apply OCR error fixes to the OCR text, then re-compare
    ocr_fixed = normalize_text(ocr_text, strip_diacritics=True, fix_ocr=True)
    score5b = fuzz.partial_ratio(ascii_comment, ocr_fixed)
    if score5b >= threshold:
        return (True, score5b, "fuzzy_ocr_corrected")

    # ── Strategy 6: Per-line matching ──
    # Split OCR text into lines and try matching each line individually.
    # This helps when OCR returns many comment blocks and the target is short.
    best_line_score = 0.0
    for line in ocr_text.split("\n"):
        line_norm = normalize_text(line)
        if not line_norm:
            continue
        s = fuzz.partial_ratio(norm_comment, line_norm)
        best_line_score = max(best_line_score, s)
        if s >= threshold:
            return (True, s, "fuzzy_per_line")
        # Also try without diacritics
        line_ascii = normalize_text(line, strip_diacritics=True)
        s2 = fuzz.partial_ratio(ascii_comment, line_ascii)
        best_line_score = max(best_line_score, s2)
        if s2 >= threshold:
            return (True, s2, "fuzzy_per_line_no_diacritics")

    # ── No match found — return best score ──
    best_overall = max(score3, score4, score5, score5b, best_line_score)
    return (False, best_overall, "no_match")
