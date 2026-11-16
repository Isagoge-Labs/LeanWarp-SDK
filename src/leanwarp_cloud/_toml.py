"""TOML reading: the standard library's parser from Python 3.11, its PyPI original before."""

import sys

if sys.version_info >= (3, 11):  # noqa: UP036 - the SDK supports Python 3.10
    from tomllib import TOMLDecodeError, loads
else:
    from tomli import TOMLDecodeError, loads

__all__ = ["TOMLDecodeError", "loads"]
