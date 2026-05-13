import asyncio
import aiohttp
import ollama
from typing import Optional

class LLMClient:
    def __init__(self, api_url: str = 'http://YOUR_OLLAMA_URL',
                 model: str = 'qwen3-coder:30b',
                 timeout: int = 30,
                 max_retries: int = 3):
        self.api_url = api_url
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = ollama.AsyncClient(host=api_url)

    async def get_completion(self, prompt: str) -> Optional[str]:
        """Get completion from Ollama API"""
        try:
            response = await self.client.chat(
                model=self.model,
                messages=[{'role': 'user', 'content': prompt}],
                options={'timeout': self.timeout}
            )
            return response['message']['content']
        except asyncio.TimeoutError:
            print(f"[LLM Client] Request timed out after {self.timeout}s")
            return None
        except Exception as e:
            print(f"[LLM Client] Error: {e}")
            return None

    async def get_completion_with_retry(self, prompt: str, max_retries: int = None) -> Optional[str]:
        """Get completion with retry logic"""
        if max_retries is None:
            max_retries = self.max_retries

        for attempt in range(max_retries):
            result = await self.get_completion(prompt)
            if result:
                return result

            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2  # Exponential backoff
                print(f"[LLM Client] Retry {attempt + 1}/{max_retries} after {wait_time}s...")
                await asyncio.sleep(wait_time)

        return None


class VLLMClient:
    """LLM client for OpenAI-compatible APIs (vLLM, Colosseum/Claude, etc.)."""

    def __init__(self, api_url: str, model: str,
                 timeout: int = 30, max_retries: int = 3,
                 endpoint_path: str = '/v1/chat/completions'):
        self.api_url = api_url.rstrip('/')
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.endpoint_path = endpoint_path

    async def get_completion(self, prompt: str) -> Optional[str]:
        """Get completion from vLLM OpenAI-compatible API."""
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.api_url}{self.endpoint_path}",
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
        except asyncio.TimeoutError:
            print(f"[vLLM Client] Request timed out after {self.timeout}s")
            return None
        except Exception as e:
            print(f"[vLLM Client] Error: {e}")
            return None

    async def get_completion_with_retry(self, prompt: str, max_retries: int = None) -> Optional[str]:
        """Get completion with retry logic."""
        if max_retries is None:
            max_retries = self.max_retries

        for attempt in range(max_retries):
            result = await self.get_completion(prompt)
            if result:
                return result

            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2
                print(f"[vLLM Client] Retry {attempt + 1}/{max_retries} after {wait_time}s...")
                await asyncio.sleep(wait_time)

        return None


def create_llm_client(llm_type: str, agent_type: str = None):
    """Factory function to create the appropriate LLM client by name.

    Args:
        llm_type: One of 'claude', 'qwen', 'vllm', 'fine-tuned'
        agent_type: Agent identifier ('l2_manager', 'power', 'scheduler', 'prb').
                    Used by 'fine-tuned' to select the per-agent LoRA adapter.
    """
    from shared.config import (LLM_CONFIG, VLLM_CONFIG, CLAUDE_LLM_CONFIG,
                                FINETUNED_BASE_URL, FINETUNED_TIMEOUT,
                                FINETUNED_MAX_RETRIES, FINETUNED_MODELS)

    if llm_type == 'claude':
        return VLLMClient(**CLAUDE_LLM_CONFIG)
    elif llm_type == 'vllm':
        return VLLMClient(**VLLM_CONFIG)
    elif llm_type == 'qwen':
        return LLMClient(**LLM_CONFIG)
    elif llm_type == 'fine-tuned':
        model = FINETUNED_MODELS.get(agent_type, 'prb_blocking_agent')
        return VLLMClient(
            api_url=FINETUNED_BASE_URL,
            model=model,
            timeout=FINETUNED_TIMEOUT,
            max_retries=FINETUNED_MAX_RETRIES,
        )
    else:
        raise ValueError(f"Unknown LLM type: {llm_type}. Use 'claude', 'qwen', 'vllm', or 'fine-tuned'.")