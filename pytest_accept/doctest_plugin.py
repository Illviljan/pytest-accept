from __future__ import annotations

import logging
import re
import textwrap
import warnings
from collections import defaultdict
from doctest import DocTestFailure
from itertools import zip_longest
from pathlib import Path

import pytest
from _pytest.doctest import DoctestItem, MultipleDoctestFailures

from . import failed_doctests_key, file_hashes_key

logger = logging.getLogger(__name__)

# StashKey-based state tracking replaces global dictionaries
# This provides proper isolation between test sessions and better testability


def pytest_collect_file(file_path, parent):
    """
    Store the hash of the file so we can check if it changed later
    """
    file_hashes = parent.session.stash.setdefault(file_hashes_key, {})
    file_hashes[file_path] = hash(file_path.read_bytes())


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    # Returning this is required by pytest.
    outcome = yield

    if not isinstance(item, DoctestItem) or not call.excinfo:
        return

    failed_doctests = item.session.stash.setdefault(
        failed_doctests_key, defaultdict(list)
    )

    if isinstance(call.excinfo.value, DocTestFailure):
        failed_doctests[Path(call.excinfo.value.test.filename)].append(
            call.excinfo.value
        )

    elif isinstance(call.excinfo.value, MultipleDoctestFailures):
        for failure in call.excinfo.value.failures:
            # Don't include tests that fail because of an error setting the test.
            if isinstance(failure, DocTestFailure):
                failed_doctests[Path(failure.test.filename)].append(failure)

    return outcome.get_result()


def _snapshot_start_line(failure: DocTestFailure) -> int:
    assert failure.test.lineno is not None
    return (
        failure.test.lineno
        + failure.example.lineno
        + len(failure.example.source.splitlines())
    )


def pytest_addoption(parser):
    """Add pytest-accept options to pytest"""
    group = parser.getgroup("accept", "accept test plugin")
    group.addoption(
        "--accept",
        action="store_true",
        default=False,
        help="Accept the output of doctests, overwriting python files with generated results.",
    )
    group.addoption(
        "--accept-copy",
        action="store_true",
        default=False,
        help="Write a copy of python file named `.py.new` with the generated results of doctests.",
    )


def pytest_configure(config):
    """Sets doctests to continue after first failure"""
    config.option.doctest_continue_on_failure = True


def _to_doctest_format(output: str) -> str:
    """
    Convert a string into a doctest format.

    For example, this requires `<BLANKLINE>`s:
    >>> print(
    ...     '''
    ... hello
    ...
    ... world
    ... '''
    ... )
    <BLANKLINE>
    hello
    <BLANKLINE>
    world

    Here, we have a doctest confirming this behavior (but we have to add a prefix, or
    it'll treat it as an actual blank line! Maybe this is pushing doctests too far!):
    >>> for line in _to_doctest_format(
    ...     '''
    ... hello
    ...
    ... world
    ... '''
    ... ).splitlines():
    ...     print(f"# {line}")
    # <BLANKLINE>
    # hello
    # <BLANKLINE>
    # world

    """

    lines = output.splitlines()
    blankline_sentinel = "<BLANKLINE>"
    transformed_lines = [line if line else blankline_sentinel for line in lines]
    # In some pathological cases, really long lines can crash an editor.
    shortened_lines = [
        line if len(line) < 1000 else f"{line[:50]}...{line[-50:]}"
        for line in transformed_lines
    ]
    # Again, only for the pathological cases.
    if len(shortened_lines) > 1000:
        shortened_lines = shortened_lines[:50] + ["..."] + shortened_lines[-50:]
    output = "\n".join(shortened_lines)
    return _redact_volatile(output)


def _redact_volatile(output: str) -> str:
    """
    Replace some volatile values, like temp paths & memory locations.

    >>> _redact_volatile("<__main__.A at 0x10b80ce50>")
    '<__main__.A at 0x...>'

    >>> _redact_volatile("/tmp/abcd234/pytest-accept-test-temp-file-0.py")
    '/tmp/.../pytest-accept-test-temp-file-0.py'

    """
    mem_locations = re.sub(r" 0x[0-9a-fA-F]+", " 0x...", output)
    temp_paths = re.sub(r"/tmp/[0-9a-fA-F]+", "/tmp/...", mem_locations)
    return temp_paths


def pytest_sessionfinish(session, exitstatus):
    """
    Write generated doctest results to their appropriate files
    """

    assert session.config.option.doctest_continue_on_failure

    passed_accept = session.config.getoption("--accept")
    passed_accept_copy = session.config.getoption("--accept-copy")
    if not (passed_accept or passed_accept_copy):
        return

    failed_doctests = session.stash.setdefault(failed_doctests_key, defaultdict(list))
    file_hashes = session.stash.setdefault(file_hashes_key, {})

    for path, failures in failed_doctests.items():
        # Check if the file has changed since the start of the test.
        current_hash = hash(path.read_bytes())
        if path not in file_hashes:
            warnings.warn(
                f"{path} not found by pytest-accept as having collected tests "
                "at the start of the session. Proceeding to overwrite. Please "
                "report an issue if this occurs unexpectedly. Full path list is "
                f"{list(file_hashes.keys())}"
            )
        elif not passed_accept_copy and current_hash != file_hashes[path]:
            logger.warning(
                f"File changed since start of test, not writing results: {path}"
            )
            continue

        # sort by line number
        failures = sorted(failures, key=lambda x: x.test.lineno or 0)

        original = list(path.read_text(encoding="utf-8").splitlines())
        path = path.with_suffix(".py.new") if passed_accept_copy else path
        with path.open("w+", encoding="utf-8") as file:
            # TODO: is there cleaner way of doing this interleaving?

            first_failure = failures[0]
            next_start_line = _snapshot_start_line(first_failure)
            for line in original[:next_start_line]:
                print(line, file=file)

            for current, next in zip_longest(failures, failures[1:]):
                # Get the existing indentation from the source line
                existing_indent = re.match(r"\s*", original[next_start_line]).group()
                snapshot_result = _to_doctest_format(current.got)
                indented = textwrap.indent(snapshot_result, prefix=existing_indent)
                for line in indented.splitlines():
                    print(line, file=file)

                current_finish_line = _snapshot_start_line(current) + len(
                    current.example.want.splitlines()
                )
                next_start_line = _snapshot_start_line(next) if next else len(original)

                for line in original[current_finish_line:next_start_line]:
                    print(line, file=file)
