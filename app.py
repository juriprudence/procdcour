import os, random, json, threading, asyncio, time, hashlib, re
from datetime import datetime

import edge_tts
import requests
from flask import Flask, jsonify, render_template, request, session, send_file

from law_lesson_app.lessons import load_lessons
from law_lesson_app.evaluator import evaluate, ask_question, check_server, get_server_url
import firebase_helper


def _load_env(env_path="android_app/env.txt"):
    if not os.path.exists(env_path):
        env_path = "env.txt"
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if m:
                key, val = m.group(1), m.group(2).strip("\"'")
                os.environ.setdefault(key, val)

_load_env()

firebase_helper.init_firebase()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())

AUDIO_DIR = os.path.abspath("static/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
PASS_SCORE = 6
LISTEN_SECONDS = 30

LESSONS = load_lessons()


def generate_all_audio():
    print("Generating audio files on startup...")
    for lesson in LESSONS:
        lid = lesson["id"]
        for lang, key in [("FR", "audioTextFr"), ("AR", "audioTextAr")]:
            text = lesson.get(key)
            if not text:
                continue
            filename = f"{lid}_{lang}.mp3"
            filepath = os.path.join(AUDIO_DIR, filename)
            if os.path.exists(filepath):
                print(f"  [SKIP] {filename}")
                continue
            print(f"  [GEN]  {filename}...")
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                voice = "fr-FR-HenriNeural" if lang == "FR" else "ar-SA-HamedNeural"
                loop.run_until_complete(edge_tts.Communicate(text, voice).save(filepath))
                loop.close()
                print(f"  [DONE] {filename}")
            except Exception as e:
                print(f"  [FAIL] {filename}: {e}")
    print("Audio generation complete.")


AUDIO_DONE_LOCK = os.path.join(AUDIO_DIR, ".done")


def ensure_audio_generated():
    if os.environ.get("GENERATE_AUDIO", "1") != "1":
        return
    if os.path.exists(AUDIO_DONE_LOCK):
        return
    generate_all_audio()
    try:
        with open(AUDIO_DONE_LOCK, "w") as f:
            f.write("done")
    except Exception:
        pass


@app.before_request
def before_request():
    ensure_audio_generated()


def _get_user_id():
    if "user_id" not in session:
        session["user_id"] = firebase_helper.generate_guest_uid()
    return session["user_id"]


def _get_user():
    uid = _get_user_id()
    try:
        user_data = firebase_helper._admin_read(f"users/{uid}")
        if user_data:
            return user_data
    except Exception:
        pass
    return {"name": "Étudiant", "email": "", "guest": True}


def _save_user(info):
    uid = _get_user_id()
    firebase_helper._admin_write(f"users/{uid}", info)


def _get_sessions():
    uid = _get_user_id()
    try:
        progressions = firebase_helper.get_progression(uid)
        return progressions
    except Exception:
        return []


def _add_session(session_data):
    uid = _get_user_id()
    now_ms = int(time.time() * 1000)
    session_data["userId"] = uid
    session_data["id"] = now_ms
    session_data["studyDate"] = now_ms
    try:
        firebase_helper.save_progression(uid, session_data)
        _update_leaderboard(uid)
    except Exception as e:
        print(f"Firebase save error: {e}")


def _clear_sessions():
    uid = _get_user_id()
    try:
        # Delete user's progression node
        for db_url in firebase_helper.DATABASE_URLS:
            url = f"{db_url.rstrip('/')}/progression/{uid}.json"
            try:
                requests.delete(url, timeout=10)
            except Exception:
                pass
        # Delete leaderboard entry
        for db_url in firebase_helper.DATABASE_URLS:
            url = f"{db_url.rstrip('/')}/leaderboard/{uid}.json"
            try:
                requests.delete(url, timeout=10)
            except Exception:
                pass
    except Exception as e:
        print(f"Firebase clear error: {e}")


def _update_leaderboard(uid):
    try:
        user_sessions = firebase_helper.get_progression(uid)
        if not user_sessions:
            return
        user_info = _get_user()
        successful = set()
        best_scores = {}
        for s in user_sessions:
            if s.get("isSuccess"):
                successful.add(s["lessonId"])
            lid = s["lessonId"]
            best_scores[lid] = max(best_scores.get(lid, 0), s.get("aiScore", 0))
        success_points = len(successful) * 100
        achievement_points = sum(best_scores.values()) * 10
        interaction_points = min(len(user_sessions) * 5, 150)
        total_score = success_points + achievement_points + interaction_points
        completed_count = len({s["lessonId"] for s in user_sessions if s.get("isSuccess")})
        display_name = user_info.get("name", "Étudiant")
        if user_info.get("guest"):
            display_name = f"Invité #{firebase_helper.get_unique_guest_number(uid)}"
        firebase_helper.submit_global_score(uid, display_name, total_score, completed_count)
    except Exception as e:
        print(f"Leaderboard update error: {e}")


def _calculate_global_score(user_sessions):
    if not user_sessions:
        return 0, 0
    successful = set()
    best_scores = {}
    for s in user_sessions:
        if s.get("isSuccess"):
            successful.add(s["lessonId"])
        lid = s["lessonId"]
        best_scores[lid] = max(best_scores.get(lid, 0), s.get("aiScore", 0))
    success_points = len(successful) * 100
    achievement_points = sum(best_scores.values()) * 10
    interaction_points = min(len(user_sessions) * 5, 150)
    return success_points + achievement_points + interaction_points, len(successful)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/lessons")
def api_lessons():
    cat = request.args.get("category")
    if cat:
        return jsonify([l for l in LESSONS if l.get("category", "").lower() == cat.lower()])
    return jsonify(LESSONS)


@app.route("/api/lesson/<lesson_id>")
def api_lesson(lesson_id):
    lesson = next((l for l in LESSONS if l["id"] == lesson_id), None)
    if not lesson:
        return jsonify({"error": "not found"}), 404
    return jsonify(lesson)


@app.route("/api/categories")
def api_categories():
    cats = list(dict.fromkeys(l.get("category", "Autre") for l in LESSONS))
    counts = {}
    for c in cats:
        counts[c] = len([l for l in LESSONS if l.get("category", "").lower() == c.lower()])
    return jsonify([{"name": c, "count": counts[c]} for c in cats])


@app.route("/api/quiz/<lesson_id>")
def api_quiz(lesson_id):
    lesson = next((l for l in LESSONS if l["id"] == lesson_id), None)
    if not lesson:
        return jsonify({"error": "not found"}), 404
    quizzes = lesson.get("quizzes", [])
    if not quizzes:
        q = {
            "id": f"{lesson_id}_Q1",
            "questionFr": lesson.get("quizQuestionFr", ""),
            "questionAr": lesson.get("quizQuestionAr", ""),
            "placeholderFr": lesson.get("quizPlaceholderFr", ""),
            "keywords": lesson.get("keywords", []),
            "correctAnswerFr": lesson.get("correctAnswerFr", ""),
            "correctAnswerAr": lesson.get("correctAnswerAr", ""),
        }
        return jsonify(q)
    return jsonify(random.choice(quizzes))


@app.route("/api/generate-audio/<lesson_id>", methods=["POST"])
def api_generate_audio(lesson_id):
    data = request.get_json(silent=True) or {}
    lang = data.get("language", "FR")
    lesson = next((l for l in LESSONS if l["id"] == lesson_id), None)
    if not lesson:
        return jsonify({"error": "not found"}), 404
    text = lesson.get("audioTextFr") if lang == "FR" else lesson.get("audioTextAr", lesson.get("audioTextFr"))
    filename = f"{lesson_id}_{lang}.mp3"
    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                edge_tts.Communicate(text, "fr-FR-HenriNeural" if lang == "FR" else "ar-SA-HamedNeural").save(filepath)
            )
            loop.close()
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"audio_url": f"/audio/{filename}"})


@app.route("/api/evaluate", methods=["POST"])
def api_evaluate():
    data = request.get_json(silent=True) or {}
    answer = data.get("answer", "")
    lesson_id = data.get("lessonId", "")
    quiz_data = data.get("quiz", {})

    lesson = next((l for l in LESSONS if l["id"] == lesson_id), None)
    if not lesson:
        return jsonify({"error": "lesson not found"}), 404

    normalized = answer.lower()
    keywords = quiz_data.get("keywords", lesson.get("keywords", []))
    matched = [kw for kw in keywords if kw.lower() in normalized]

    keyword_score = min(len(matched) / max(len(keywords), 1) * 5, 5)
    question_fr = quiz_data.get("questionFr", lesson.get("quizQuestionFr", ""))
    hint = ", ".join(keywords)

    try:
        ai_result, feedback = evaluate(question_fr, answer, hint)
    except Exception:
        ai_result, feedback = 3, "Évaluation automatique effectuée."

    ai_score = min(ai_result, 5)
    final_score = round(keyword_score + ai_score)
    final_score = max(0, min(final_score, 10))

    passed = final_score >= PASS_SCORE

    session_entry = {
        "lessonId": lesson_id,
        "lessonTitleFr": lesson.get("titleFr", ""),
        "lessonTitleAr": lesson.get("titleAr", ""),
        "audioDurationSeconds": data.get("elapsed", 30),
        "audioLanguage": data.get("language", "FR"),
        "userAnswer": answer,
        "aiScore": final_score,
        "aiEvaluation": feedback,
        "isSuccess": passed,
        "matchedKeywords": matched,
    }
    _add_session(session_entry)

    return jsonify({
        "score": final_score,
        "feedback": feedback,
        "passed": passed,
        "keywordsMatched": matched,
        "keywordsTotal": keywords,
        "correctAnswerFr": quiz_data.get("correctAnswerFr", lesson.get("correctAnswerFr", "")),
        "correctAnswerAr": quiz_data.get("correctAnswerAr", lesson.get("correctAnswerAr", "")),
    })


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.get_json(silent=True) or {}
    question = data.get("question", "")
    lesson_id = data.get("lessonId", "")
    lesson = next((l for l in LESSONS if l["id"] == lesson_id), None) if lesson_id else None
    context = ""
    if lesson:
        context = f"{lesson.get('titleFr', '')}\n\n{lesson.get('audioTextFr', '')}"
    try:
        response = ask_question(question, context)
    except Exception as e:
        response = f"Erreur: {e}"
    return jsonify({"response": response})


@app.route("/api/sessions")
def api_sessions():
    sessions = _get_sessions()
    return jsonify(sorted(sessions, key=lambda s: s.get("studyDate", 0), reverse=True))


@app.route("/api/sessions/clear", methods=["POST"])
def api_clear_sessions():
    _clear_sessions()
    return jsonify({"status": "ok"})


@app.route("/api/user/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "")
    password = data.get("password", "")
    name = data.get("name", email.split("@")[0] if "@" in email else "Étudiant")
    try:
        result = firebase_helper.create_user_auth(email, password, name)
        uid = result.get("localId", result.get("uid", ""))
        if not uid:
            return jsonify({"error": "Échec de création du compte"}), 400
        firebase_helper._admin_write(f"users/{uid}", {
            "name": name, "email": email, "guest": False,
        })
        session["user_id"] = uid
        return jsonify({"status": "ok", "user": {"name": name, "email": email}})
    except requests.HTTPError as e:
        error_msg = str(e)
        if "EMAIL_EXISTS" in error_msg:
            return jsonify({"error": "Cet email est déjà utilisé"}), 400
        return jsonify({"error": f"Erreur d'inscription: {error_msg}"}), 400
    except Exception as e:
        return jsonify({"error": f"Erreur: {str(e)}"}), 500


@app.route("/api/user/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "")
    password = data.get("password", "")
    try:
        result = firebase_helper.sign_in_with_email(email, password)
        uid = result.get("localId", "")
        session["user_id"] = uid
        user_data = firebase_helper._admin_read(f"users/{uid}")
        name = user_data.get("name", email.split("@")[0] if "@" in email else "Étudiant")
        firebase_helper._admin_write(f"users/{uid}", {
            "name": name, "email": email, "guest": False,
        })
        return jsonify({"status": "ok", "user": {"name": name, "email": email}})
    except requests.HTTPError:
        return jsonify({"error": "Email ou mot de passe incorrect"}), 401
    except Exception as e:
        return jsonify({"error": f"Erreur: {str(e)}"}), 500


@app.route("/api/user/logout", methods=["POST"])
def api_logout():
    session.pop("user_id", None)
    return jsonify({"status": "ok"})


@app.route("/api/user/status")
def api_user_status():
    uid = _get_user_id()
    info = _get_user()
    sessions = _get_sessions()
    total_score, completed = _calculate_global_score(sessions)
    return jsonify({
        "id": uid,
        "name": info.get("name", "Étudiant"),
        "email": info.get("email", ""),
        "guest": info.get("guest", True),
        "totalScore": total_score,
        "completedCount": completed,
    })


@app.route("/api/leaderboard")
def api_leaderboard():
    try:
        lb = firebase_helper.get_top_scores(10)
        return jsonify(lb)
    except Exception:
        return jsonify([])


@app.route("/api/stats")
def api_stats():
    sessions = _get_sessions()
    total = len(sessions)
    success = [s for s in sessions if s.get("isSuccess")]
    success_rate = round(len(success) * 100 / max(total, 1))
    avg_score = round(sum(s.get("aiScore", 0) for s in sessions) / max(total, 1), 1) if total > 0 else 0

    cat_stats = {}
    for cat_name in list(dict.fromkeys(l.get("category", "Autre") for l in LESSONS)):
        cat_lessons = [l for l in LESSONS if l.get("category") == cat_name]
        completed_ids = {s["lessonId"] for s in success}
        completed = sum(1 for l in cat_lessons if l["id"] in completed_ids)
        cat_stats[cat_name] = {"total": len(cat_lessons), "completed": completed}

    return jsonify({
        "total": total,
        "successCount": len(success),
        "successRate": success_rate,
        "averageScore": avg_score,
        "categories": cat_stats,
    })


@app.route("/api/check-server")
def api_check_server():
    url = get_server_url()
    ok = check_server(url)
    return jsonify({"available": ok})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    _clear_sessions()
    return jsonify({"status": "ok"})


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "not found"}), 404
    return send_file(filepath, mimetype="audio/mpeg")


@app.route("/favicon.ico")
def favicon():
    return "", 204


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)
