import asyncio
from search import _serper_search

async def main():
    res = await _serper_search(["image", "upscaling", "github", "python"])
    print(res)

asyncio.run(main())
