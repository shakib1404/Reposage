import asyncio
from dotenv import load_dotenv
load_dotenv()
from search import _serper_search, _rank_repos, _build_keywords

async def main():
    task = "image upscaling"
    keywords = _build_keywords(task, "image")
    raw = await _serper_search(keywords)
    print("Raw from serper:", len(raw))
    print(raw[:1])
    ranked = await _rank_repos(raw, task)
    print("Ranked:", ranked)

asyncio.run(main())
