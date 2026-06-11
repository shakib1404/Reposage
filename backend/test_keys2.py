import asyncio
from search import SERPER_KEY
import httpx

async def main():
    keywords = ['image', 'upscaling', 'github', 'python']
    query = " ".join(keywords[:4]) + " github python deep learning site:github.com"
    headers = {"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"}
    payload = {"q": query, "num": 10}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post("https://google.serper.dev/search", headers=headers, json=payload)
        data = resp.json()
        print(len(data.get("organic", [])))
        for item in data.get("organic", []):
            print(item.get("link"))

asyncio.run(main())
