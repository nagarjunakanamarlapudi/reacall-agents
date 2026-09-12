"""The shared SDK callback construction seam cannot override private invocation policy."""

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
