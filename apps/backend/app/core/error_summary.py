"""
An error as one line that never repeats the value it refused.

A pydantic error's text quotes each refused input (`input_value=...`), which for
a check is a target or connection string, credentials and all. Where an error
is kept for others to read (a job's progress), it is summarised from each
error's location and message instead.
"""

from pydantic import ValidationError


def validation_summary(error: ValidationError) -> str:
    """`error` as `field: message` pairs, without the inputs."""
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or 'value'}: {err['msg']}"
        for err in error.errors(include_input=False)
    )


def error_summary(error: Exception) -> str:
    """`error` in one line; a validation error without the inputs it refused."""
    if isinstance(error, ValidationError):
        return validation_summary(error)
    return str(error)
