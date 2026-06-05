import os
import sys
import random
import json
import threading
import asyncio

import edge_tts
from flask import Flask, jsonify, render_template, request, session, send_file

from law_lesson_app.lessons import load_lessons
from law_lesson_app.evaluator import evaluate, ask_question, check_server, get_server_url

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

AUDIO_DIR = os.path.abspath("static/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
PASS_SCORE = 6
_VOICE = "fr-FR-HenriNeural"

lessons = load_lessons()
_audio_cache = {}


def _generate_all_audio():
    total = sum(len(l["sub_lessons"]) for l in lessons)
    done = 0
    sys.stdout.reconfigure(encoding='utf-8')
    print(f"[AUDIO] Generation de {total} sous-lecons...")
    for li, lesson in enumerate(lessons):
        for si, sub in enumerate(lesson["sub_lessons"]):
            filename = f"lesson_{li}_{si}.mp3"
            filepath = os.path.join(AUDIO_DIR, filename)
            if not os.path.exists(filepath) or os.path.getsize(filepath) < 1000:
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(
                        edge_tts.Communicate(sub["text"], _VOICE).save(filepath)
                    )
                    loop.close()
                except Exception as e:
                    print(f"  [ERR] {sub['title']}: {e}")
            done += 1
            print(f"  [OK] [{done}/{total}] {sub['title']}")
    print("[AUDIO] Tous les fichiers audio sont prets!")


_generate_all_audio()


def _total_sub_count():
    return sum(len(l["sub_lessons"]) for l in lessons)


def _running_sub_total(lesson_idx, sub_idx):
    return sum(len(l["sub_lessons"]) for l in lessons[:lesson_idx]) + sub_idx + 1


def _get_lesson_data(lesson_idx, sub_idx):
    lesson = lessons[lesson_idx]
    sub = lesson["sub_lessons"][sub_idx]
    return {
        "lesson_idx": lesson_idx,
        "sub_idx": sub_idx,
        "lesson_title": lesson["title"],
        "lesson_num": lesson_idx + 1,
        "total_lessons": len(lessons),
        "sub_title": sub["title"],
        "sub_num": sub_idx + 1,
        "sub_count": len(lesson["sub_lessons"]),
        "running_total": _running_sub_total(lesson_idx, sub_idx),
        "total_sub": _total_sub_count(),
        "text": sub["text"],
        "question_count": len(sub["questions"]),
    }


def _get_passed():
    return set(tuple(x) for x in session.get("passed", []))


def _set_passed(li, si):
    passed = set(tuple(x) for x in session.get("passed", []))
    passed.add((li, si))
    session["passed"] = [list(p) for p in passed]


def _is_next_blocked(li, si):
    passed = _get_passed()
    return (li, si) not in passed


@app.route("/")
def index():
    session.setdefault("lesson_idx", 0)
    session.setdefault("sub_idx", 0)
    session.setdefault("passed", [])
    return render_template("index.html")


@app.route("/api/lesson")
def api_lesson():
    li = session.get("lesson_idx", 0)
    si = session.get("sub_idx", 0)
    li = max(0, min(li, len(lessons) - 1))
    si = max(0, min(si, len(lessons[li]["sub_lessons"]) - 1))
    session["lesson_idx"] = li
    session["sub_idx"] = si
    data = _get_lesson_data(li, si)
    data["question"] = _pick_question(li, si)
    data["is_passed"] = (li, si) in _get_passed()
    return jsonify(data)


def _pick_question(lesson_idx, sub_idx):
    sub = lessons[lesson_idx]["sub_lessons"][sub_idx]
    q = random.choice(sub["questions"])
    session["question_idx"] = sub["questions"].index(q)
    return {"q": q["q"], "hint": q["hint"]}


@app.route("/api/navigate", methods=["POST"])
def api_navigate():
    direction = request.json.get("direction", "next")
    li = session.get("lesson_idx", 0)
    si = session.get("sub_idx", 0)

    blocked = _is_next_blocked(li, si)

    if direction == "next":
        if blocked:
            return jsonify({"blocked": True, "reason": "Vous devez réussir le quiz avant de passer à la suite."}), 403
        if si < len(lessons[li]["sub_lessons"]) - 1:
            si += 1
        elif li < len(lessons) - 1:
            li += 1
            si = 0
        else:
            return jsonify({"done": True})
    elif direction == "prev":
        if si > 0:
            si -= 1
        elif li > 0:
            li -= 1
            si = len(lessons[li]["sub_lessons"]) - 1
    elif direction == "goto":
        tli = request.json.get("lesson_idx", li)
        tsi = request.json.get("sub_idx", si)
        if (tli, tsi) != (li, si) and (tli, tsi) not in _get_passed():
            if tli > li or (tli == li and tsi > si):
                return jsonify({"blocked": True, "reason": "Vous devez réussir le quiz précédent."}), 403
        li, si = tli, tsi

    li = max(0, min(li, len(lessons) - 1))
    si = max(0, min(si, len(lessons[li]["sub_lessons"]) - 1))
    session["lesson_idx"] = li
    session["sub_idx"] = si

    data = _get_lesson_data(li, si)
    data["question"] = _pick_question(li, si)
    data["is_passed"] = (li, si) in _get_passed()
    return jsonify(data)


@app.route("/api/generate-audio", methods=["POST"])
def api_generate_audio():
    li = session.get("lesson_idx", 0)
    si = session.get("sub_idx", 0)
    filename = f"lesson_{li}_{si}.mp3"
    return jsonify({"audio_url": f"/audio/{filename}"})


@app.route("/api/evaluate", methods=["POST"])
def api_evaluate():
    data = request.json
    answer = data.get("answer", "")
    li = session.get("lesson_idx", 0)
    si = session.get("sub_idx", 0)
    sub = lessons[li]["sub_lessons"][si]
    qi = session.get("question_idx", 0)
    q = sub["questions"][qi]

    try:
        score, feedback = evaluate(q["q"], answer, q["hint"])
    except Exception as e:
        score, feedback = 0, f"Erreur: {e}"

    is_last_sub = si >= len(lessons[li]["sub_lessons"]) - 1
    is_last_lesson = li >= len(lessons) - 1
    is_last = is_last_sub and is_last_lesson

    passed = score >= PASS_SCORE
    if passed:
        _set_passed(li, si)

    return jsonify({
        "score": score,
        "feedback": feedback,
        "passed": passed,
        "is_last": is_last,
        "is_last_sub": is_last_sub,
        "is_last_lesson": is_last_lesson,
    })


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.json
    question = data.get("question", "")
    li = session.get("lesson_idx", 0)
    si = session.get("sub_idx", 0)
    sub = lessons[li]["sub_lessons"][si]
    lesson_context = f"{sub['title']}\n\n{sub['text']}"

    try:
        response = ask_question(question, lesson_context)
    except Exception as e:
        response = f"Erreur: {e}"

    return jsonify({"response": response})


@app.route("/api/check-server")
def api_check_server():
    url = get_server_url()
    ok = check_server(url)
    return jsonify({"available": ok})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    session["lesson_idx"] = 0
    session["sub_idx"] = 0
    session["passed"] = []
    return jsonify({"status": "ok"})


@app.route("/api/sub-lessons")
def api_sub_lessons():
    passed = _get_passed()
    result = []
    for li, lesson in enumerate(lessons):
        for si, sub in enumerate(lesson["sub_lessons"]):
            result.append({
                "lesson_idx": li,
                "sub_idx": si,
                "lesson_title": lesson["title"],
                "sub_title": sub["title"],
                "passed": (li, si) in passed,
            })
    return jsonify(result)


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "not found"}), 404
    return send_file(filepath, mimetype="audio/mpeg")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5500))
    app.run(debug=False, host="0.0.0.0", port=port)
