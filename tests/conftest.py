"""pytest configuration for the direct-mode suite.

gltest 0.29.2 injects the contract message context by ``dup2``-ing a temp file
onto stdin (fd 0) and then immediately ``os.unlink``-ing it. On POSIX that is
safe (an open file may be unlinked and stays readable through the descriptor),
but on Windows the unlink raises ``PermissionError`` because fd 0 keeps the file
open until the VM restores stdin after the call in ``gltest.direct.vm``.

This shim makes the harness work on Windows by deferring each injected temp
file's unlink until the following call, by which point stdin has been restored
and no descriptor references the file any more, plus once at interpreter exit.
On non-Windows platforms the module is left completely untouched, so the
documented Linux workflow behaves exactly as before.
"""

import atexit
import os

if os.name == "nt":
    from gltest.direct import loader as _loader

    _pending: list[str] = []

    def _flush_pending() -> None:
        remaining = []
        for path in _pending:
            try:
                os.unlink(path)
            except OSError:
                remaining.append(path)
        _pending[:] = remaining

    def _inject_message_to_fd0(vm) -> None:
        # Mirror of gltest.direct.loader._inject_message_to_fd0 with the eager
        # unlink replaced by deferred cleanup so it is valid on Windows. The
        # genlayer imports are lazy because the SDK path is only injected into
        # sys.path once the direct VM is active, exactly as in the original.
        try:
            from genlayer.py import calldata
            from genlayer.py.types import Address
        except ImportError:
            return

        sender_addr = vm.sender
        if isinstance(sender_addr, bytes):
            sender_addr = Address(sender_addr)

        contract_addr = vm._contract_address
        if isinstance(contract_addr, bytes):
            contract_addr = Address(contract_addr)

        origin_addr = vm.origin
        if isinstance(origin_addr, bytes):
            origin_addr = Address(origin_addr)

        message_data = {
            "contract_address": contract_addr,
            "sender_address": sender_addr,
            "origin_address": origin_addr,
            "stack": [],
            "value": vm._value,
            "datetime": vm._datetime,
            "is_init": False,
            "chain_id": vm._chain_id,
            "entry_kind": 0,
            "entry_data": b"",
            "entry_stage_data": None,
        }

        encoded = calldata.encode(message_data)

        # Any temp file from the previous call is now unreferenced: stdin has
        # been restored, so deleting it cannot raise on Windows.
        _flush_pending()

        import tempfile

        fd, path = tempfile.mkstemp()
        try:
            os.write(fd, encoded)
            os.lseek(fd, 0, os.SEEK_SET)
            original_stdin = os.dup(0)
            vm._original_stdin_fd = original_stdin
            os.dup2(fd, 0)
        finally:
            os.close(fd)

        _pending.append(path)

    _loader._inject_message_to_fd0 = _inject_message_to_fd0
    atexit.register(_flush_pending)
