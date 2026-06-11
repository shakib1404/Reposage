import asyncio
from dotenv import load_dotenv
load_dotenv()
from search import search_repos

async def main():
    res = await search_repos("image upscaling")
    print(res)

asyncio.run(main())
