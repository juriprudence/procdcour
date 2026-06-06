import json
import requests

_SYSTEM_PROMPT = "Tu es un professeur de droit qui note des étudiants."


def get_server_url(path="server.txt"):
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
    for endpoint in ("/health", "/v1/models", "/api/tags", "/"):
        try:
            r = requests.get(f"{url}{endpoint}", headers=headers, timeout=5)
            if r.status_code < 500:
                return True
        except Exception:
            continue
    return False


def _keyword_fallback(answer, hint, reason=""):
    # Flexible matching: compare first 5 chars of each word
    import re
    kw_parts = [re.sub(r"[^a-z0-9]", "", w.strip().lower()) for w in hint.split(",") if w.strip()]
    ans_lower = re.sub(r"[^a-z0-9]", "", answer.lower())
    hits = 0
    for kw in kw_parts:
        # Match if keyword stem (first 5+ chars) appears in answer
        stem = kw[:5]
        if len(stem) >= 3 and stem in ans_lower:
            hits += 1
            continue
        # Also try exact match
        if kw and kw in ans_lower:
            hits += 1
    total = max(len(kw_parts), 1)
    score = min(10, round((hits / total) * 10))
    label = f"(Sans IA – {reason} – " if reason else "(Sans IA – "
    return score, f"{label}mots-clés : {hits}/{total})"


def _search_lesson(question, lesson_context):
    import re
    words = [w.lower() for w in re.findall(r"\w{4,}", question)]
    if not words:
        return "Posez une question plus précise sur la leçon."
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", lesson_context) if p.strip()]
    scored = []
    for p in paragraphs:
        p_lower = p.lower()
        hits = sum(1 for w in words if w in p_lower)
        if hits > 0:
            scored.append((hits, len(p), p))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = [p for _, _, p in scored[:3]]
    if best:
        text = "\n\n".join(best)
        return f"Extrait de la leçon :\n\n{text}" if len(text) > 20 else "Le serveur IA n'est pas disponible. Réessayez plus tard."
    return "Le serveur IA n'est pas disponible. Réessayez plus tard."


def ask_question(question, lesson_context, server_url=None):
    if server_url is None:
        server_url = get_server_url()

    prompt = (
        f"Contexte de la leçon :\n{lesson_context}\n\n"
        f"Question de l'étudiant : {question}\n"
        "En tant que professeur de droit, réponds à la question en utilisant tes connaissances générales. "
        "Le contexte ci-dessus est un support supplémentaire. "
        "Réponds en français en 2-4 phrases claires et complètes."
    )

    payload = {
        "prompt": prompt,
        "n_predict": 500,
        "temperature": 0.7,
    }

    try:
        endpoint = f"{server_url.rstrip('/')}/v1/completions"
        headers = {"ngrok-skip-browser-warning": "true"} if "ngrok" in server_url else {}
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
        if resp.status_code != 200:
            return _search_lesson(question, lesson_context)
        body = resp.json()
        answer = body.get("choices", [{}])[0].get("text", "").strip()
        if not answer:
            return _search_lesson(question, lesson_context)
        return answer
    except Exception as e:
        return _search_lesson(question, lesson_context)


def evaluate(question, answer, hint, server_url=None):
    if server_url is None:
        server_url = get_server_url()

    if not check_server(server_url):
        return _keyword_fallback(answer, hint, reason="serveur indisponible")

    prompt = (
        "Question: quelle est la capitale de la France?\n"
        "Etudiant: Berlin\n"
        "Attendu: Paris\n"
        "Note: 0\n"
        "\n"
        "Question: role du juge?\n"
        "Etudiant: il applique la loi\n"
        "Attendu: appliquer la loi, rendre la justice\n"
        "Note: 7\n"
        "\n"
        "Question: role du juge?\n"
        "Etudiant: il dirige les enquetes\n"
        "Attendu: diriger les enquetes\n"
        "Note: 10\n"
        "\n"
        f"Question: {question}\n"
        f"Etudiant: {answer}\n"
        f"Attendu: {hint}\n"
        "Note:"
    )

    payload = {
        "prompt": prompt,
        "n_predict": 10,
        "temperature": 0.3,
        "stop": ["\n"],
    }

    try:
        endpoint = f"{server_url.rstrip('/')}/v1/completions"
        headers = {"ngrok-skip-browser-warning": "true"} if "ngrok" in server_url else {}
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
        if resp.status_code != 200:
            return _keyword_fallback(answer, hint, reason=f"HTTP {resp.status_code}")
        body = resp.json()
        raw = body.get("choices", [{}])[0].get("text", "").strip()
        import re
        nums = re.findall(r"\b(\d{1,2})\b", raw)
        score_from_llm = max(0, min(10, int(nums[0]))) if nums else None
        kw_score, _ = _keyword_fallback(answer, hint)
        score = score_from_llm if score_from_llm is not None else kw_score
        tag = "(IA)" if score_from_llm is not None else "(mots-clés)"
        return score, f"{tag} Score: {score}/10"
    except Exception as e:
        return _keyword_fallback(answer, hint, reason=str(e))
