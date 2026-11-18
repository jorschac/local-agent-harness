import httpx
import pytest
from openai import AsyncOpenAI
from harness.errors import HarnessError
from harness.paths import init_home, load_config
from harness.provider import OpenAIProvider

@pytest.mark.asyncio
async def test_region_denial_is_not_bad_key_and_does_not_retry(tmp_path):
    init_home(tmp_path)
    count = 0
    async def reject(request):
        nonlocal count
        count += 1
        return httpx.Response(403,json={"error":{"code":403,"message":"This model is not available in your region.","metadata":{"private":"synthetic-secret"}}})
    provider = OpenAIProvider(tmp_path, load_config(tmp_path))
    provider.client = AsyncOpenAI(api_key="synthetic-test-key",base_url="https://mock.invalid/v1",max_retries=0,
                                 http_client=httpx.AsyncClient(transport=httpx.MockTransport(reject)))
    try:
        with pytest.raises(HarnessError) as caught:
            await provider.complete([{"role":"user","content":"test"}],[],8)
        assert caught.value.code == "provider_region_unavailable"
        assert "synthetic-secret" not in caught.value.body()["error"]["message"]
        assert count == 1
    finally:
        await provider.close()
