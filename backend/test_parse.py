import json
import re

def _parse_json(text: str):
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        idx = text.find(start_char)
        if idx != -1:
            end = text.rfind(end_char)
            if end != -1:
                return json.loads(text[idx:end + 1])
    return json.loads(text)

text = """{
  "repos": [
    {
      "full_name": "topics/image-super-resolution",
      "name": "image-super-resolution"
    }
  ]
}"""

print(type(_parse_json(text)))
print(_parse_json(text))
