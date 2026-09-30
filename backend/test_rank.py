import asyncio
import pathlib

from dotenv import load_dotenv

# backend/.env only holds SERPER/JINA keys and would shadow the real one,
# so load the project-root .env explicitly.
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

from search import search_repos



async def main():
    res = await search_repos("image upscaling")
    print("normalized :", res["query"])
    print("suggestions:", res["suggestions"])
    for i, r in enumerate(res["repos"], 1):
        print(f"{i}. {r['full_name']:<50} score={r['score']:<6} "
              f"stars={r['stars']:<7} run={r['runnable']} {r['run_tags']}")


asyncio.run(main())
