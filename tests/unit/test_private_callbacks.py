"""The shared SDK callback construction seam cannot override private invocation policy."""

import subprocess
import sys
from textwrap import dedent

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from recallops.llm.live_reasoning import _SafeCallbacks
from recallops.llm.private_callbacks import private_callbacks


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_private_model_callbacks_ignore_explicit_verbose_and_foreign_handlers(
    capsys, asynchronous
):
    observed = []

    class ForeignHandler(BaseCallbackHandler):
        def on_llm_end(self, *args, **kwargs):
            observed.append("foreign callback executed")

    async def invoke(content, callbacks):
        model = GenericFakeChatModel(
            messages=iter([AIMessage(content=content)]), verbose=True, callbacks=[ForeignHandler()]
        )
        if asynchronous:
            return await model.ainvoke(content, config={"callbacks": callbacks})
        return model.invoke(content, config={"callbacks": callbacks})

    safe = _SafeCallbacks("test-model")
    private = "PRIVATE_CALLBACK_CONSTRUCTION_CANARY"
    with private_callbacks(safe):
        result = await invoke(private, [ForeignHandler()])
    assert result.content == private
    assert observed == []
    assert safe.model_calls == 1 and len(safe.events) == 1
    captured = capsys.readouterr()
    leaked = private in captured.out + captured.err
    assert not leaked
    # Context exit restores normal callback construction, not global settings.
    await invoke("PUBLIC", [ForeignHandler()])
    assert len(observed) == 2
    assert safe.model_calls == 1


@pytest.mark.parametrize("reload_count", [1, 3])
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("manager_name", ["CallbackManager", "AsyncCallbackManager"])
def test_fresh_process_reload_preserves_sdk_and_private_context(reload_count, active, manager_name):
    script = dedent(
        """
        import importlib
        import sys
        from contextlib import nullcontext
        from langchain_core.callbacks import BaseCallbackHandler
        from langchain_core.callbacks import manager as managers
        from langchain_core.globals import get_debug, get_verbose, set_debug, set_verbose

        original = managers._configure
        import recallops.llm.private_callbacks as module
        binding = module._PRIVATE_CALLBACK
        private, foreign = BaseCallbackHandler(), BaseCallbackHandler()
        set_debug(True)
        set_verbose(True)
        manager_types = (getattr(managers, sys.argv[3]),)
        expected = {
            cls: original(cls, [foreign], verbose=True, inheritable_tags=["PUBLIC"])
            for cls in manager_types
        }
        context = module.private_callbacks(private) if sys.argv[2] == "True" else nullcontext()
        old_wrapper = managers._configure
        with context:
            for _ in range(int(sys.argv[1])):
                importlib.reload(module)
                for cls in manager_types:
                    configured = cls.configure(
                        inheritable_callbacks=[foreign], verbose=True,
                        inheritable_tags=["PUBLIC"]
                    )
                    if sys.argv[2] == "True":
                        assert configured.handlers == [private]
                        assert configured.inheritable_handlers == [private]
                        assert binding.get() is private
                    else:
                        assert [type(h) for h in configured.handlers] == [
                            type(h) for h in expected[cls].handlers
                        ]
                        assert configured.handlers[0] is foreign
                        assert configured.tags == expected[cls].tags
                assert module._PRIVATE_CALLBACK is binding
                assert module._sdk_configure is original
                assert module._sdk_configure is not managers._configure
                # An already-referenced wrapper must also avoid self-delegation.
                old_wrapper(managers.CallbackManager)
        assert binding.get() is None
        assert module._PRIVATE_CALLBACK.get() is None
        for cls in manager_types:
            configured = cls.configure(
                inheritable_callbacks=[foreign], verbose=True, inheritable_tags=["PUBLIC"]
            )
            assert [type(h) for h in configured.handlers] == [
                type(h) for h in expected[cls].handlers
            ]
            assert configured.handlers[0] is foreign
            assert configured.tags == expected[cls].tags
        assert (get_debug(), get_verbose()) == (True, True)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(reload_count), str(active), manager_name],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-800:]
