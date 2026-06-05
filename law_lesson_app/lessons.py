import json


def load_lessons(path="lesson.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
