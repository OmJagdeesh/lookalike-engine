"""GSTIN format and mod-36 weighted checksum validator."""

from __future__ import annotations

import re
from dataclasses import dataclass

# 36-character base alphabet used for GSTIN mod-36 checksum
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_CHAR_TO_VAL = {ch: idx for idx, ch in enumerate(_ALPHABET)}

# Official 15-character GSTIN structure:
# [2-digit state][10-char PAN: 5 alpha + 4 digit + 1 alpha][1 entity][Z][1 checksum]
_GSTIN_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")

_MIN_STATE_CODE = 1
_MAX_STATE_CODE = 38


@dataclass(frozen=True)
class ValidationResult:
    """Structured result from GSTIN validation."""

    is_valid: bool
    normalized: str
    error_code: str | None = None
    error_message: str | None = None
    state_code: str | None = None
    pan: str | None = None


def compute_gstin_checksum(prefix14: str) -> str:
    """Compute the 15th GSTIN check character using the official mod-36 weighted sum."""
    if len(prefix14) != 14:
        raise ValueError(f"Expected 14-character prefix, got {len(prefix14)}")

    normalized = prefix14.strip().upper()
    total = 0
    for idx, ch in enumerate(normalized):
        if ch not in _CHAR_TO_VAL:
            raise ValueError(f"Invalid character {ch!r} in GSTIN prefix")
        code_point = _CHAR_TO_VAL[ch]
        factor = 1 if idx % 2 == 0 else 2
        product = code_point * factor
        digit = (product // 36) + (product % 36)
        total += digit

    remainder = total % 36
    check_val = (36 - remainder) % 36
    return _ALPHABET[check_val]


def validate_gstin(gstin: str | None) -> ValidationResult:
    """Validate an Indian 15-character GSTIN (format, state code, and mod-36 checksum)."""
    if gstin is None:
        return ValidationResult(
            is_valid=False,
            normalized="",
            error_code="EMPTY_GSTIN",
            error_message="GSTIN is missing or empty.",
        )

    normalized = str(gstin).strip().upper()
    if not normalized:
        return ValidationResult(
            is_valid=False,
            normalized="",
            error_code="EMPTY_GSTIN",
            error_message="GSTIN is missing or empty.",
        )

    if len(normalized) != 15:
        return ValidationResult(
            is_valid=False,
            normalized=normalized,
            error_code="INVALID_LENGTH",
            error_message=f"GSTIN must be 15 characters, got {len(normalized)}.",
        )

    if not _GSTIN_REGEX.match(normalized):
        return ValidationResult(
            is_valid=False,
            normalized=normalized,
            error_code="INVALID_FORMAT",
            error_message="GSTIN does not match the required 15-character PAN/entity pattern.",
        )

    state_num = int(normalized[:2])
    if not (_MIN_STATE_CODE <= state_num <= _MAX_STATE_CODE):
        return ValidationResult(
            is_valid=False,
            normalized=normalized,
            error_code="INVALID_STATE_CODE",
            error_message=f"GSTIN state code {normalized[:2]!r} is outside valid range 01-38.",
        )

    expected_checksum = compute_gstin_checksum(normalized[:14])
    if normalized[14] != expected_checksum:
        return ValidationResult(
            is_valid=False,
            normalized=normalized,
            error_code="INVALID_CHECKSUM",
            error_message=(
                f"GSTIN checksum mismatch: expected {expected_checksum!r}, got {normalized[14]!r}."
            ),
        )

    return ValidationResult(
        is_valid=True,
        normalized=normalized,
        state_code=normalized[:2],
        pan=normalized[2:12],
    )
