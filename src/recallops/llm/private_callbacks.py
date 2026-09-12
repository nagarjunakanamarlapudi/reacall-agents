"""Invocation-local callback construction without process-global tracing changes."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.callbacks import manager as callback_managers

_PRIVATE_CALLBACK: ContextVar[BaseCallbackHandler | None] = ContextVar(
    "recallops_private_callback", default=None
)
_sdk_configure = callback_managers._configure


@wraps(_sdk_configure)
def _configure(callback_manager_cls, inheritable_callbacks=None, *args, **kwargs):
    callback = _PRIVATE_CALLBACK.get()
    if callback is None:
        return _sdk_configure(callback_manager_cls, inheritable_callbacks, *args, **kwargs)
    # Both SDK manager classes use this construction seam. Do not invoke the SDK's
    # ambient/global handler discovery inside a private investigation: even a
    # legitimate concurrent set_debug(True) would otherwise install raw tracing.
    # Preserve lineage only from this invocation's own sanitized callback manager.
    parent_run_id = None
    if isinstance(inheritable_callbacks, callback_managers.BaseCallbackManager) and any(
        handler is callback for handler in inheritable_callbacks.handlers
    ):
        parent_run_id = inheritable_callbacks.parent_run_id
    return callback_manager_cls(
        handlers=[callback], inheritable_handlers=[callback], parent_run_id=parent_run_id
    )


# Install once on import, not on context entry/exit. Outside private invocations
# the original SDK behavior is unchanged, including concurrent caller settings.
callback_managers._configure = _configure


@contextmanager
def private_callbacks(callback: BaseCallbackHandler) -> Iterator[None]:
    """Allow only the supplied sanitized callback for this task and its children."""
    token = _PRIVATE_CALLBACK.set(callback)
    try:
        yield
    finally:
        _PRIVATE_CALLBACK.reset(token)
