import asyncio
from dotenv import load_dotenv
load_dotenv()
from search import groq, GROQ_CHAT_MODEL

async def main():
    task = "image upscaling"
    listing = "1. topics/image-super-resolution — Super-scale your images..."
    resp = await groq.chat.completions.create(
        model=GROQ_CHAT_MODEL,
        temperature=0.15,
        max_tokens=600,
        messages=[
            {"role": "system", "content":
                "Rank GitHub repos for a task. Return ONLY valid JSON with this shape: "
                '{"repos": [{"full_name":"...","name":"...","owner":"...",'
                '"stars":"12k","language":"Python","description":"...",'
                '"icon":"🔧","score":9.2}]}'},
            {"role": "user", "content": f"Task: {task}\n\nRepos:\n{listing}"},
        ],
    )
    print(resp.choices[0].message.content)

asyncio.run(main())
