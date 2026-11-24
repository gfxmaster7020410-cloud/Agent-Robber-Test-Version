import os
import asyncio
from typing import AsyncGenerator, Optional
from google import genai
from google.genai import types

class GeminiStreamEngine:
    """Async engine for streaming Gemini responses using google-genai SDK."""

    def __init__(self, api_key: Optional[str] = None):
        key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        self.client = genai.Client(api_key=key) if key else None

    async def stream_completion(
        self,
        prompt_payload: str,
        system_instruction: str = "",
        temperature: float = 0.2,
        model: str = "gemini-3.6-flash"
    ) -> AsyncGenerator[str, None]:
        """Streams completion from Gemini model."""
        if self.client is None:
            return
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature
        )
        
        try:
            response_stream = await self.client.aio.models.generate_content_stream(
                model=model,
                contents=prompt_payload,
                config=config
            )
            async for chunk in response_stream:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            # Log exception securely
            print(f"Error in Gemini streaming: {e}")
            raise

if __name__ == "__main__":
    # Debugging/Testing
    async def main():
        try:
            engine = GeminiStreamEngine()
            print("Streaming test...")
            async for chunk in engine.stream_completion("Hello, how are you?"):
                print(chunk, end="", flush=True)
            print("\nDone.")
        except Exception as e:
            print(f"Test failed: {e}")

    asyncio.run(main())
