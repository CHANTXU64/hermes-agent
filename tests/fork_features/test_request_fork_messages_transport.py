"""Exercise provider SDKs, the physical stream seam and host hook payloads."""
import copy
import json
from types import SimpleNamespace
from contextlib import nullcontext

import httpx
import pytest

from fork_features.request_fork import FrozenRequest, RequestForkService, current_request_fork_scope
from fork_features.request_fork.message_protocols import send_messages, freeze_anthropic_client_factory


@pytest.mark.parametrize("mode", ["anthropic_messages", "chat_completions"])
def test_sdk_stream_collects_text_tools_usage_and_progress(mode):
    seen, ticks = [], []
    if mode == "chat_completions":
        from openai import OpenAI
        chunks = [
            {"choices": [{"index":0,"delta":{"role":"assistant","content":"JSON"},"finish_reason":None}]},
            {"choices": [{"index":0,"delta":{"tool_calls":[{"index":0,"id":"t","type":"function","function":{"name":"read","arguments":"{\"x\":"}}]},"finish_reason":None}]},
            {"choices": [{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}]},
            {"choices": [],"usage":{"prompt_tokens":100,"completion_tokens":10,"total_tokens":110}},
        ]
        wire = ''.join('data: '+json.dumps({"id":"c","object":"chat.completion.chunk","created":1,"model":"m",**x})+'\n\n' for x in chunks)+'data: [DONE]\n\n'
        build = OpenAI
    else:
        from anthropic import Anthropic
        chunks = [
            {"type":"message_start","message":{"id":"m","type":"message","role":"assistant","model":"m","content":[],"stop_reason":None,"stop_sequence":None,"usage":{"input_tokens":100,"output_tokens":0}}},
            {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}},
            {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"JSON"}},
            {"type":"content_block_stop","index":0},
            {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t","name":"read","input":{}}},
            {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"x\":1}"}},
            {"type":"content_block_stop","index":1},
            {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":None},"usage":{"output_tokens":10}},
            {"type":"message_stop"},
        ]
        wire = ''.join('event: '+x['type']+'\ndata: '+json.dumps(x)+'\n\n' for x in chunks)
        build = Anthropic
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, content=wire)
    with build(api_key="test",base_url="https://provider.invalid",http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        response=send_messages(client,{"model":"m","messages":[{"role":"user","content":"prefix"}],"max_tokens":100}, mode=mode,progress=lambda:ticks.append(1))
    if mode == "chat_completions":
        assert response.choices[0].message.content == "JSON"
        assert response.choices[0].message.tool_calls[0].function.arguments == '{"x":1}'
        assert response.usage.prompt_tokens == 100
    else:
        assert response.content[0].text == "JSON"
        assert response.content[1].input == {"x":1}
        assert response.usage.input_tokens == 100
    assert ticks and seen[0]["messages"] == [{"role":"user","content":"prefix"}]


def test_anthropic_factory_captures_route_without_retaining_parent(monkeypatch):
    import weakref, gc
    calls=[]
    class Agent:
        key=("direct","original-key","https://original.invalid",23,True)
        def _request_anthropic_client_key(self): return self.key
    a=Agent()
    monkeypatch.setattr("agent.anthropic_adapter.build_anthropic_client",lambda *args,**kw:calls.append((args,kw)))
    factory=freeze_anthropic_client_factory(a)
    ref=weakref.ref(a)
    a.key=("direct","new-key","https://new.invalid",5,False)
    del a; gc.collect()
    assert ref() is None
    factory()
    assert calls==[(("original-key","https://original.invalid"),{"timeout":23,"drop_context_1m_beta":True})]


@pytest.mark.parametrize("mode", ["anthropic_messages", "chat_completions"])
def test_lifecycle_publishes_native_request_messages(monkeypatch,mode):
    from fork_features.request_fork.compression_lifecycle import CompressionLifecycle
    body={"model":"m","messages":[{"role":"user","content":"native with request-only context"}],"tools":[]}
    frozen=FrozenRequest(body=body,api_mode=mode,fidelity="prepared_parent")
    seen=[]
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook",lambda _:True)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook",lambda name,**kwargs:seen.append((name,kwargs)))
    monkeypatch.setattr("fork_features.request_fork.current_request_fork_scope",lambda *a,**kw:nullcontext())
    agent=SimpleNamespace(session_id="s",api_mode=mode,tools=[])
    lifecycle=CompressionLifecycle(agent,attempt_id="c",in_place=True,trigger_source="test",request_fork=frozen,
                                   request_fork_rematerializer=None,commit_fence=None,defer_finish=False)
    lifecycle.start([{"role":"user","content":"durable"}])
    assert seen[0][1]["request_messages"]==body["messages"]


@pytest.mark.parametrize("mode", ["anthropic_messages", "chat_completions"])
def test_stream_overflow_capture_observes_final_wire_body(monkeypatch, mode):
    from unittest.mock import MagicMock
    from agent.chat_completion_helpers import _StreamingCall
    agent=MagicMock()
    agent.api_mode=mode
    agent.base_url="https://provider.invalid"
    agent.provider="test"
    agent.model="m"
    agent._stream_options_unsupported=True
    body={"model":"m","messages":[{"role":"user","content":"after middleware"}],"max_tokens":1024}
    call=_StreamingCall(agent,copy.deepcopy(body),None)
    captured=[]
    call.on_physical_request=lambda payload:captured.append(copy.deepcopy(payload))
    if mode=="chat_completions":
        client=MagicMock()
        client.chat.completions.create.side_effect=RuntimeError("overflow")
        agent._create_request_openai_client.return_value=client
        call.clients.set_client=lambda c:c
        with pytest.raises(RuntimeError,match="overflow"):
            call._open_chat_stream(copy.deepcopy(body))
    else:
        client=MagicMock()
        client.messages.stream.side_effect=RuntimeError("overflow")
        def relay(payload,send,**kwargs):
            payload=copy.deepcopy(payload)
            payload["messages"][0]["content"]="relay replaced payload"
            return send(payload)
        monkeypatch.setattr("agent.relay_llm.stream",relay)
        with pytest.raises(RuntimeError,match="overflow"):
            call._call_anthropic(client)
    assert len(captured)==1
    assert captured[0]["messages"][0]["content"]==("relay replaced payload" if mode=="anthropic_messages" else "after middleware")
