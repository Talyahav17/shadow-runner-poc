"""Parses a COBOL program's LINKAGE SECTION into structured field specs.

Before this, every field width (LOAN_INT_DIGITS, RATE_DEC_DIGITS, etc.) in
field_specs.py was hand-typed by reading interest_calc.cbl -- which only
scales to one program. This lets main.py's proxy and migrate.py support
any COBOL program whose LINKAGE SECTION uses unsigned DISPLAY numeric
fields, by parsing the PIC clauses directly out of the source.

SCOPE, STATED HONESTLY: the ctypes bridge in main.py only ever supported
unsigned DISPLAY (zoned decimal) numeric fields -- PIC 9(n)V9(m) -- because
that's what can be safely encoded/decoded as plain ASCII digit buffers.
Signed DISPLAY fields (PIC S9(n)V9(m)) are now also supported -- see
cobol_proxy.py for the empirically-derived ASCII sign-overpunch encoding
GnuCOBOL uses for the last digit. COMP/COMP-3/BINARY (packed or binary
storage), OCCURS (tables), and alphanumeric (PIC X) fields are still
DETECTED and explicitly rejected with a clear error naming the offending
field, rather than silently mis-encoded. A loud "unsupported" error here
is far safer than a wrong number silently reaching a shadow comparison.
"""

import re
from dataclasses import dataclass


class UnsupportedFieldError(ValueError):
    """A LINKAGE SECTION field uses a construct this bridge can't safely
    encode/decode. See module docstring SCOPE note."""


@dataclass
class FieldSpec:
    name: str
    int_digits: int
    dec_digits: int
    signed: bool = False

    @property
    def width(self) -> int:
        return self.int_digits + self.dec_digits

    def max_value_str(self) -> str:
        """Exact decimal string for the largest value this field can hold
        (see field_specs.py's max_value_str, which this mirrors)."""
        if self.dec_digits:
            return f"{'9' * self.int_digits}.{'9' * self.dec_digits}"
        return "9" * self.int_digits


def _extract_linkage_section(source: str) -> str:
    match = re.search(
        r"LINKAGE\s+SECTION\s*\.(.*?)(?=PROCEDURE\s+DIVISION|\Z)",
        source, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError("No LINKAGE SECTION found in COBOL source")
    return match.group(1)


def _count_digits(segment: str) -> int:
    """'9(7)' -> 7, '99' -> 2, '9(2)9' -> 3."""
    total = 0
    for group in re.findall(r"9(?:\((\d+)\))?", segment):
        total += int(group) if group else 1
    return total


def _parse_numeric_pic(pic: str) -> tuple:
    """'9(7)V99' -> (7, 2). Handles both repeat-count '9(n)' and literal
    '999' forms, and both 'V' (implied decimal point) and a literal '.'
    as the decimal separator."""
    pic = pic.upper()
    sep_match = re.search(r"[V.]", pic)
    if sep_match:
        int_part, dec_part = pic[:sep_match.start()], pic[sep_match.end():]
    else:
        int_part, dec_part = pic, ""
    return _count_digits(int_part), _count_digits(dec_part)


def parse_linkage_fields(cobol_source: str) -> list:
    """Returns a list of FieldSpec, one per supported 01-level elementary
    numeric field in the LINKAGE SECTION, in declaration order.

    Raises UnsupportedFieldError (naming the offending field) for any
    field using a construct this bridge can't safely handle -- see the
    module docstring's SCOPE note.
    """
    linkage_text = _extract_linkage_section(cobol_source)
    normalized = re.sub(r"\s+", " ", linkage_text).strip()
    statements = [s.strip() for s in normalized.split(".") if s.strip()]

    fields = []
    for stmt in statements:
        m = re.match(r"01\s+([A-Za-z0-9-]+)\s+(.*)", stmt, re.IGNORECASE)
        if not m:
            continue
        name, rest = m.group(1), m.group(2)

        if re.search(r"\bOCCURS\b", rest, re.IGNORECASE):
            raise UnsupportedFieldError(f"{name}: OCCURS (table) fields are not supported by this bridge")

        usage_match = re.search(
            r"\b(COMP-3|COMP-2|COMP-1|COMP|COMPUTATIONAL-3|COMPUTATIONAL|BINARY|PACKED-DECIMAL)\b",
            rest, re.IGNORECASE,
        )
        if usage_match:
            raise UnsupportedFieldError(
                f"{name}: USAGE {usage_match.group(1)} (packed/binary storage) is not supported -- "
                f"only DISPLAY (zoned decimal, the COBOL default) numeric fields are."
            )

        pic_match = re.search(r"PIC(?:TURE)?\s+(?:IS\s+)?(\S+?)\s*$", rest, re.IGNORECASE)
        if not pic_match:
            pic_match = re.search(r"PIC(?:TURE)?\s+(?:IS\s+)?(\S+)", rest, re.IGNORECASE)
        if not pic_match:
            raise UnsupportedFieldError(
                f"{name}: no PIC clause found (group items with nested sub-fields aren't supported)"
            )
        pic = pic_match.group(1).rstrip(".")

        if re.search(r"[XA]", pic, re.IGNORECASE):
            raise UnsupportedFieldError(f"{name}: alphanumeric PIC '{pic}' is not supported -- numeric fields only")

        signed = pic.upper().startswith("S")
        pic_digits = pic[1:] if signed else pic

        int_digits, dec_digits = _parse_numeric_pic(pic_digits)
        if int_digits == 0 and dec_digits == 0:
            raise UnsupportedFieldError(f"{name}: could not parse PIC clause '{pic}' as a numeric field")

        fields.append(FieldSpec(name=name, int_digits=int_digits, dec_digits=dec_digits, signed=signed))

    if not fields:
        raise ValueError("No supported 01-level numeric fields found in LINKAGE SECTION")

    return fields


def parse_program_id(cobol_source: str) -> str:
    match = re.search(r"PROGRAM-ID\.\s+([A-Za-z0-9-]+)", cobol_source, re.IGNORECASE)
    if not match:
        raise ValueError("No PROGRAM-ID found in COBOL source")
    return match.group(1)


def parse_using_clause(cobol_source: str) -> list:
    """Returns the LINKAGE field names in calling order, from
    'PROCEDURE DIVISION USING X Y Z.' -- this is the actual parameter
    order the compiled entry point expects, which is not guaranteed to
    match LINKAGE SECTION declaration order (though it usually does)."""
    match = re.search(
        r"PROCEDURE\s+DIVISION\s+USING\s+(.*?)\.",
        cobol_source, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError("No 'PROCEDURE DIVISION USING ...' clause found in COBOL source")
    names = re.findall(r"[A-Za-z0-9-]+", match.group(1))
    if not names:
        raise ValueError("'PROCEDURE DIVISION USING' clause had no parameter names")
    return names


def entry_point_symbol(program_id: str) -> str:
    """GnuCOBOL's C-callable entry point name for a given PROGRAM-ID:
    empirically, hyphens become double underscores (see main.py's
    CobolInterestCalculator docstring for how this was discovered)."""
    return program_id.upper().replace("-", "__")
