"""Loads programs.yaml and builds a ready-to-call GenericCobolProxy +
modern Python function for each registered legacy program.

This is what makes main.py support more than one COBOL program: adding a
program is editing programs.yaml, not main.py.
"""

import importlib
import os
from dataclasses import dataclass

import yaml

import cobol_parser
from cobol_proxy import GenericCobolProxy

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "programs.yaml")


class ProgramRegistrationError(RuntimeError):
    """Raised when a programs.yaml entry can't be loaded -- e.g. its COBOL
    source uses an unsupported field type, or input_params doesn't match
    the program's actual parameter count."""


@dataclass
class RegisteredProgram:
    name: str
    display_name: str
    proxy: GenericCobolProxy
    modern_fn: callable
    input_params: list       # friendly API field names, in calling order
    input_fields: list       # cobol_parser.FieldSpec, same order as input_params
    result_field: object     # cobol_parser.FieldSpec


def _load_one_program(entry: dict, base_dir: str) -> RegisteredProgram:
    name = entry["name"]
    cobol_path = os.path.join(base_dir, entry["cobol_source"])
    so_path = os.path.join(base_dir, entry["shared_library"])

    try:
        cobol_source = open(cobol_path).read()
    except OSError as exc:
        raise ProgramRegistrationError(f"{name}: could not read COBOL source at {cobol_path}: {exc}") from exc

    try:
        linkage_fields = cobol_parser.parse_linkage_fields(cobol_source)
        program_id = cobol_parser.parse_program_id(cobol_source)
        using_order = cobol_parser.parse_using_clause(cobol_source)
    except (ValueError, cobol_parser.UnsupportedFieldError) as exc:
        raise ProgramRegistrationError(f"{name}: {exc}") from exc

    fields_by_cobol_name = {f.name: f for f in linkage_fields}

    input_params = entry["input_params"]
    if len(input_params) != len(using_order) - 1:
        raise ProgramRegistrationError(
            f"{name}: programs.yaml lists {len(input_params)} input_params, but "
            f"'PROCEDURE DIVISION USING' has {len(using_order)} parameters "
            f"(expected {len(using_order) - 1} inputs + 1 result)"
        )

    input_cobol_names = using_order[:-1]
    result_cobol_name = using_order[-1]

    try:
        input_fields = [fields_by_cobol_name[cobol_name] for cobol_name in input_cobol_names]
        result_field = fields_by_cobol_name[result_cobol_name]
    except KeyError as exc:
        raise ProgramRegistrationError(
            f"{name}: 'PROCEDURE DIVISION USING' references {exc}, which has no "
            f"matching (supported) LINKAGE SECTION field"
        ) from exc

    entry_symbol = cobol_parser.entry_point_symbol(program_id)
    try:
        proxy = GenericCobolProxy(so_path, entry_symbol, input_fields, result_field)
    except AttributeError as exc:
        raise ProgramRegistrationError(
            f"{name}: shared library at {so_path} has no symbol '{entry_symbol}' "
            f"(expected from PROGRAM-ID {program_id})"
        ) from exc

    modern_module = importlib.import_module(entry["modern_module"])
    modern_fn = getattr(modern_module, entry["modern_function"])

    return RegisteredProgram(
        name=name,
        display_name=entry.get("display_name", name),
        proxy=proxy,
        modern_fn=modern_fn,
        input_params=input_params,
        input_fields=input_fields,
        result_field=result_field,
    )


def load_registry(config_path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Returns {program_name: RegisteredProgram} for every entry in
    programs.yaml. Fails loudly (ProgramRegistrationError) rather than
    silently skipping a misconfigured program -- a shadow-runner that
    silently drops a program is worse than one that refuses to start."""
    base_dir = os.path.dirname(os.path.abspath(config_path))
    with open(config_path) as f:
        config = yaml.safe_load(f)

    registry = {}
    for entry in config.get("programs", []):
        program = _load_one_program(entry, base_dir)
        registry[program.name] = program
    return registry
