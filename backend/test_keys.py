import asyncio
from search import _classify_task, _build_keywords, _serper_search
from dotenv import load_dotenv
load_dotenv()

async def main():
    task = "image upscaling"
    task_type = await _classify_task(task)
    print("Type:", task_type)
    keywords = _build_keywords(task, task_type)
    print("Keywords:", keywords)
    raw = await _serper_search(keywords)
    print("Raw from serper:", len(raw))
    print(raw)

asyncio.run(main())
