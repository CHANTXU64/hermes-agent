"""Fork contract: Hindsight retain payloads keep Unicode readable."""

from tests.plugins.memory.test_hindsight_provider import provider


def test_auto_retain_payload_contains_real_unicode_characters(provider):
    provider.sync_turn("中文问题", "中文回答")
    provider._retain_queue.join()

    item = provider._client.aretain_batch.call_args.kwargs["items"][0]
    assert "中文问题" in item["content"]
    assert "中文回答" in item["content"]
    assert "\\u4e2d" not in item["content"]
