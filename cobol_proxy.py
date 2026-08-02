"""Generic ctypes bridge to a compiled GnuCOBOL shared library.

Extracted from main.py's original CobolInterestCalculator (which only
ever worked for interest_calc.so) so any program described by
cobol_parser.py's FieldSpecs can be called the same way, driven by
program_registry.py.
"""

import ctypes
import os
import threading

from cobol_parser import FieldSpec


class CobolLibraryError(RuntimeError):
    pass


# Empirically-derived (not guessed -- see SHADOW_RUNNER.md / session notes):
# GnuCOBOL's ASCII-runtime sign representation for a signed DISPLAY field
# encodes the sign into the LAST digit only, by compiling a program that
# computes a genuine negative value via `COMPUTE ... = ... * -1` and
# reading back the raw bytes it produced. Every digit before the last
# stays a plain ASCII digit regardless of sign; only the last digit's
# byte changes when the value is negative, per this table:
#   0 -> '0' (no distinct negative-zero byte was observed)
#   1-9 -> chr(ord('p') + d), i.e. 'q'..'y'
_NEG_LAST_DIGIT = {str(d): ("0" if d == 0 else chr(ord("p") + d)) for d in range(10)}
_NEG_LAST_DIGIT_REVERSE = {v: k for k, v in _NEG_LAST_DIGIT.items()}


def encode_fixed_point(value: float, field: FieldSpec) -> bytes:
    """Pack a float into COBOL DISPLAY (zoned decimal) format.

    A PIC 9(n)V9(m) DISPLAY field is stored as (n + m) ASCII digit bytes
    with no sign and no decimal point -- the point position is implied by
    the PIC clause, so encoding it is just scaling to an integer and
    zero-padding. A PIC S9(n)V9(m) field is identical except the sign of
    a negative value is overpunched onto the last digit -- see
    _NEG_LAST_DIGIT above.
    """
    scaled = round(value * (10 ** field.dec_digits))
    max_value = 10 ** field.width - 1
    if field.signed:
        if abs(scaled) > max_value:
            raise ValueError(f"value {value} does not fit PIC S9({field.int_digits})V9({field.dec_digits})")
        digits = f"{abs(scaled):0{field.width}d}"
        if scaled < 0:
            digits = digits[:-1] + _NEG_LAST_DIGIT[digits[-1]]
        return digits.encode("ascii")

    if scaled < 0 or scaled > max_value:
        raise ValueError(f"value {value} does not fit PIC 9({field.int_digits})V9({field.dec_digits})")
    return f"{scaled:0{field.width}d}".encode("ascii")


def decode_fixed_point(raw: bytes, field: FieldSpec) -> float:
    """Inverse of encode_fixed_point: ASCII digit bytes -> float."""
    digits = raw[:field.width].decode("ascii")
    if field.signed and digits and digits[-1] in _NEG_LAST_DIGIT_REVERSE:
        digits = digits[:-1] + _NEG_LAST_DIGIT_REVERSE[digits[-1]]
        return -(int(digits) / (10 ** field.dec_digits))
    return int(digits) / (10 ** field.dec_digits)


def canonicalize_to_field_precision(value: float, dec_digits: int) -> float:
    """Round a value down to what a PIC 9(n)V9(dec_digits) field can hold.

    The legacy COBOL field only has room for `dec_digits` fractional
    digits, so any extra precision in the caller's input is silently
    dropped the moment it's encoded for COBOL. The shadow (Python) path
    must see the exact same rounded value -- otherwise it "sees" precision
    the legacy system never had, and disagrees with production on inputs
    that were never actually equal once encoded.
    """
    scale = 10 ** dec_digits
    return round(value * scale) / scale


class GenericCobolProxy:
    """ctypes wrapper around one compiled COBOL program's shared library.

    Legacy COBOL programs compiled without RECURSIVE on their PROGRAM-ID
    get a single process-global WORKING-STORAGE instance shared across
    every call -- concurrent calls from FastAPI's threadpool crash the
    whole process (libcob detects the reentrant call and aborts). A real
    migration usually can't recompile or touch the legacy binary at all,
    so a lock serializes every call into the library here, trading
    COBOL-call throughput for correctness -- the same conservative
    assumption you'd make fronting any opaque legacy system whose
    internal thread-safety is unknown. (Discovered the hard way with
    interest_calc.so -- see test_concurrency_stress.py.)
    """

    def __init__(self, lib_path: str, entry_symbol: str, input_fields: list, result_field: FieldSpec):
        if not os.path.exists(lib_path):
            raise CobolLibraryError(f"COBOL shared library not found at {lib_path}")
        self.input_fields = input_fields
        self.result_field = result_field
        # Absolute path, not whatever was passed in: on Linux, ctypes.CDLL
        # treats a bare relative filename (no '/' in it) as a *system*
        # library name and searches the dynamic linker's standard paths
        # instead of the current directory -- it will not find a plain
        # "interest_calc.so" sitting right next to the script. macOS's
        # dyld is more permissive here, which is exactly why this bug
        # (in an earlier version of migrate.py, which passed a bare
        # relative Path) went unnoticed on a Mac and only surfaced when
        # actually tested in a Linux container.
        self._lib = ctypes.CDLL(os.path.abspath(lib_path))
        self._lib.cob_init(0, None)  # must run once before any module call
        self._entry_point = getattr(self._lib, entry_symbol)
        self._entry_point.restype = ctypes.c_int
        self._lock = threading.Lock()

    def calculate(self, *values: float) -> float:
        if len(values) != len(self.input_fields):
            raise ValueError(f"expected {len(self.input_fields)} input(s), got {len(values)}")

        buffers = [
            ctypes.create_string_buffer(encode_fixed_point(v, f), f.width)
            for v, f in zip(values, self.input_fields)
        ]
        result_buf = ctypes.create_string_buffer(self.result_field.width)

        with self._lock:
            ret_code = self._entry_point(*buffers, result_buf)
            if ret_code != 0:
                raise CobolLibraryError(f"COBOL entry point returned non-zero status {ret_code}")
            return decode_fixed_point(result_buf.raw, self.result_field)
