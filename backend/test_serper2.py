import asyncio
import httpx
import os
from dotenv import load_dotenv

load_dotenv()
SERPER_KEY = os.getenv("SERPER_API_KEY", "")

async def main():
    query = "image upscaling python github python deep learning site:github.com"
    headers = {"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"}
    payload = {"q": query, "num": 10}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post("https://google.serper.dev/search", headers=headers, json=payload)
        print(resp.status_code)
        print(resp.json())

asyncio.run(main())
