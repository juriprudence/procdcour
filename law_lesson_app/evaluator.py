import json
import os
import re
import requests


def get_server_url(path="server.txt"):
    env_url = os.environ.get("LLM_BASE_URL")
    if env_url and env_url != "MY_LLM_BASE_URL" and env_url.startswith("http"):
        return env_url
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "http://localhost:8080"


def check_server(server_url=None):
    if server_url is None:
        server_url = get_server_url()
    url = server_url.rstrip("/")
    headers = {"ngrok-skip-browser-warning": "true"} if "ngrok" in server_url else {}
    for endpoint in ("/health", "/v1/models", "/"):
        try:
            r = requests.get(f"{url}{endpoint}", headers=headers, timeout=5)
            if r.status_code < 500:
                return True
        except Exception:
            continue
    return False


def _keyword_fallback(answer, hint, reason=""):
    kw_parts = [re.sub(r"[^a-z0-9]", "", w.strip().lower()) for w in hint.split(",") if w.strip()]
    ans_lower = re.sub(r"[^a-z0-9]", "", answer.lower())
    hits = 0
    for kw in kw_parts:
        stem = kw[:5]
        if len(stem) >= 3 and stem in ans_lower:
            hits += 1
            continue
        if kw and kw in ans_lower:
            hits += 1
    total = max(len(kw_parts), 1)
    score = min(10, round((hits / total) * 10))
    label = f"(Sans IA – {reason} – " if reason else "(Sans IA – "
    return score, f"{label}mots-clés : {hits}/{total})"


def _try_gemini_api(question, answer, hint):
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or api_key == "MY_GEMINI_API_KEY":
        return None, None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    prompt = (
        "Tu es un professeur de droit français. Évalue la réponse de l'étudiant.\n\n"
        f"Question : {question}\n"
        f"Mots-clés attendus : {hint}\n"
        f"Réponse de l'étudiant : \"{answer}\"\n\n"
        "Attribue une note de 0 à 10.\n"
        "Réponds UNIQUEMENT au format JSON : {\"score\": <0-10>, \"feedback\": \"<explication en français>\"}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 200}
    }
    try:
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        json_match = re.search(r"\{[^}]+\}", text)
        if json_match:
            result = json.loads(json_match.group())
            score = int(result.get("score", 0))
            feedback = result.get("feedback", "Évaluation par IA effectuée.")
            return max(0, min(score, 10)), feedback
        nums = re.findall(r"\b(\d{1,2})\b", text)
        if nums:
            return max(0, min(10, int(nums[0]))), f"(IA Gemini) Score: {int(nums[0])}/10"
    except Exception:
        pass
    return None, None


def _try_llm_server(question, answer, hint, server_url):
    url = server_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if "ngrok" in server_url:
        headers["ngrok-skip-browser-warning"] = "true"
    model = os.environ.get("LLM_MODEL_NAME", "gemma-4-e2b-q4")
    prompt = (
        "Tu es un professeur de droit français. Évalue la réponse de l'étudiant.\n\n"
        f"Question : {question}\n"
        f"Mots-clés attendus : {hint}\n"
        f"Réponse de l'étudiant : \"{answer}\"\n\n"
        "Note sur 10. Réponds UNIQUEMENT par un nombre entier entre 0 et 10."
    )
    chat_payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 10
    }
    try:
        resp = requests.post(f"{url}/v1/chat/completions", json=chat_payload, headers=headers, timeout=60)
        if resp.status_code == 200:
            raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            nums = re.findall(r"\b(\d{1,2})\b", raw)
            if nums:
                s = max(0, min(10, int(nums[0])))
                return s, f"(IA serveur) Score: {s}/10"
    except Exception:
        pass
    try:
        comp_payload = {"prompt": prompt, "n_predict": 5, "temperature": 0.1, "stop": ["\n"]}
        resp = requests.post(f"{url}/v1/completions", json=comp_payload, headers=headers, timeout=60)
        if resp.status_code == 200:
            raw = resp.json().get("choices", [{}])[0].get("text", "").strip()
            nums = re.findall(r"\b(\d{1,2})\b", raw)
            if nums:
                s = max(0, min(10, int(nums[0])))
                return s, f"(IA serveur) Score: {s}/10"
    except Exception:
        pass
    return None, None


def ask_question(question, lesson_context, server_url=None):
    if server_url is None:
        server_url = get_server_url()
    if not check_server(server_url):
        return _keyword_fallback(question, lesson_context[:50], reason="serveur indisponible")[1]
    prompt = (
        "Tu es un professeur de droit. Tu réponds uniquement aux questions "
        "qui concernent la leçon en cours. Si la question n'est pas liée à la leçon, "
        "tu réponds : 'Cette question ne fait pas partie de la leçon actuelle.'\n\n"
        f"Contenu de la leçon actuelle :\n{lesson_context}\n\n"
        f"Question de l'étudiant : {question}\n"
        "Réponse :"
    )
    headers = {"ngrok-skip-browser-warning": "true"} if "ngrok" in server_url else {}
    try:
        endpoint = f"{server_url.rstrip('/')}/v1/completions"
        resp = requests.post(endpoint, json={"prompt": prompt, "n_predict": 300, "temperature": 0.3, "stop": ["\n\n", "Question :"]}, headers=headers, timeout=60)
        if resp.status_code != 200:
            return f"Erreur serveur : HTTP {resp.status_code}"
        answer = resp.json().get("choices", [{}])[0].get("text", "").strip()
        return answer if answer else "Le serveur n'a pas fourni de réponse."
    except Exception as e:
        return _keyword_fallback(question, lesson_context[:50], reason=str(e))[1]


def evaluate(question, answer, hint):
    try:
        score, feedback = _try_gemini_api(question, answer, hint)
        if score is not None:
            return score, feedback
    except Exception:
        pass
    try:
        server_url = get_server_url()
        if check_server(server_url):
            score, feedback = _try_llm_server(question, answer, hint, server_url)
            if score is not None:
                return score, feedback
    except Exception:
        pass
    return _keyword_fallback(answer, hint, reason="serveurs indisponibles")
