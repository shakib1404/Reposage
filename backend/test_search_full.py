import asyncio
import pathlib

from dotenv import load_dotenv

# backend/.env only holds SERPER/JINA keys and would shadow the real one,
# so load the project-root .env explicitly.
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

from search import search_repos



async def main():
    res = await search_repos("image upscaling")
    print(res)


asyncio.run(main())
