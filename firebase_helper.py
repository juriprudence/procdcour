import os
import json
import time
import hashlib
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# --- Firebase configuration (same as Android app FirebaseManager.kt) ---
FIREBASE_API_KEY = "AIzaSyD9W-UTTsZljt7P-_CRMUJ8MEarokBBZq8"
FIREBASE_PROJECT_ID = "courdroit"
FIREBASE_APP_ID = "1:1063897433594:android:c9321fd58bfe3cd90d9043"
FIREBASE_STORAGE_BUCKET = "courdroit.firebasestorage.app"
FIREBASE_DATABASE_URL = "https://courdroit-default-rtdb.firebaseio.com"

# Fallback database URLs (matching Android's possibleDatabaseUrls)
DATABASE_URLS = [
    FIREBASE_DATABASE_URL,
    "https://courdroit-default-rtdb.europe-west1.firebasedatabase.app",
    "https://courdroit-default-rtdb.asia-southeast1.firebasedatabase.app",
    "https://courdroit.firebaseio.com",
    "https://courdroit.europe-west1.firebasedatabase.app",
]

_admin_db = None


def init_firebase():
    """Initialize Firebase Admin SDK if a service account is available.
    Falls back to REST API mode if no service account credentials exist.
    """
    global _admin_db

    service_account_path = os.environ.get(
        "FIREBASE_SERVICE_ACCOUNT_PATH",
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
    )
    if service_account_path and os.path.exists(service_account_path):
        try:
            import firebase_admin
            from firebase_admin import credentials, db

            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred, {
                "databaseURL": FIREBASE_DATABASE_URL,
                "projectId": FIREBASE_PROJECT_ID,
                "storageBucket": FIREBASE_STORAGE_BUCKET,
            })
            _admin_db = db
            logger.info("Firebase Admin SDK initialized with service account.")
            return True
        except Exception as e:
            logger.warning(f"Failed to init Firebase Admin SDK: {e}")

    try:
        import firebase_admin
        from firebase_admin import db as fb_db

        if not firebase_admin._apps:
            firebase_admin.initialize_app(options={
                "databaseURL": FIREBASE_DATABASE_URL,
                "projectId": FIREBASE_PROJECT_ID,
            })
        # Test if credentials actually work by doing a simple read
        try:
            fb_db.reference("_test_").get()
            _admin_db = fb_db
            logger.info("Firebase Admin SDK initialized (fallback mode).")
            return True
        except Exception:
            logger.warning("Firebase Admin SDK initialized but credentials not available, using REST API.")
            _admin_db = None
            return False
    except Exception as e:
        logger.warning(f"Firebase Admin SDK unavailable: {e}")
        logger.info("Firebase helper will use REST API fallback.")
        return False


# ---- Firebase Auth REST API (works with only the API key) ----

def _auth_url(endpoint: str) -> str:
    return f"https://identitytoolkit.googleapis.com/v1/accounts:{endpoint}?key={FIREBASE_API_KEY}"


def sign_up_with_email(email: str, password: str) -> dict:
    """Create a new user with email/password via Firebase Auth REST API.
    Returns the response dict with idToken, localId, email, etc.
    Raises requests.HTTPError on failure.
    """
    resp = requests.post(
        _auth_url("signUp"),
        json={"email": email, "password": password, "returnSecureToken": True},
    )
    if not resp.ok:
        error_data = resp.json().get("error", {}).get("message", "Unknown error")
        raise requests.HTTPError(f"Firebase signUp failed: {error_data}", response=resp)
    return resp.json()


def sign_in_with_email(email: str, password: str) -> dict:
    """Sign in with email/password via Firebase Auth REST API.
    Returns the response dict with idToken, localId, email, etc.
    """
    resp = requests.post(
        _auth_url("signInWithPassword"),
        json={"email": email, "password": password, "returnSecureToken": True},
    )
    if not resp.ok:
        error_data = resp.json().get("error", {}).get("message", "Unknown error")
        raise requests.HTTPError(f"Firebase signIn failed: {error_data}", response=resp)
    return resp.json()


def get_user_by_email(email: str) -> Optional[dict]:
    """Look up a user by email via Firebase Auth REST API (admin).
    Requires a service account or admin SDK to be initialized.
    Falls back to the Admin SDK if available.
    """
    try:
        import firebase_admin.auth as admin_auth

        user = admin_auth.get_user_by_email(email)
        return {"uid": user.uid, "email": user.email, "displayName": user.display_name}
    except Exception:
        pass
    return None


def create_user_auth(email: str, password: str, display_name: str = "") -> dict:
    """Try Admin SDK first, fall back to REST API."""
    try:
        import firebase_admin.auth as admin_auth

        user = admin_auth.create_user(
            email=email,
            password=password,
            display_name=display_name or None,
        )
        return {"localId": user.uid, "email": user.email}
    except Exception:
        pass
    return sign_up_with_email(email, password)


# ---- Firebase Realtime Database REST API ----

_DATABASE_SECRET = os.environ.get("FIREBASE_DATABASE_SECRET", "")


def _db_url(path: str, db_url: str = FIREBASE_DATABASE_URL) -> str:
    return f"{db_url.rstrip('/')}/{path.lstrip('/')}.json"


def _db_auth():
    """Return auth token for REST API calls: database secret > nothing."""
    if _DATABASE_SECRET:
        return _DATABASE_SECRET
    return ""


def _try_write(path: str, data: dict, id_token: str = "") -> bool:
    """Write to all database URLs (same as Android's multi-region fallback)."""
    success = False
    auth_token = id_token or _db_auth()
    for db_url in DATABASE_URLS:
        url = _db_url(path, db_url)
        params = {}
        if auth_token:
            params["auth"] = auth_token
        try:
            resp = requests.put(url, params=params, json=data, timeout=10)
            if resp.ok:
                success = True
            else:
                logger.debug(f"DB write failed for {db_url}: {resp.status_code}")
        except Exception as e:
            logger.debug(f"DB write error for {db_url}: {e}")
    return success


def _try_read(path: str, id_token: str = "") -> dict:
    """Read from database URLs (returns first successful response)."""
    auth_token = id_token or _db_auth()
    for db_url in DATABASE_URLS:
        url = _db_url(path, db_url)
        params = {}
        if auth_token:
            params["auth"] = auth_token
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.ok:
                data = resp.json()
                if data is not None:
                    return data
        except Exception:
            continue
    return {}


def _try_read_query(path: str, order_by: str, limit: int, id_token: str = "") -> list:
    """Query database with ordering (first successful response)."""
    auth_token = id_token or _db_auth()
    for db_url in DATABASE_URLS:
        url = _db_url(path, db_url)
        params = {"orderBy": f'"{order_by}"', "limitToLast": limit}
        if auth_token:
            params["auth"] = auth_token
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.ok:
                data = resp.json()
                if data:
                    if isinstance(data, dict):
                        return list(data.values())
                    return data if isinstance(data, list) else []
        except Exception:
            continue
    return []


# ---- Admin SDK wrappers (fall back to REST API) ----

def _admin_write(path: str, data: dict):
    """Write using Admin SDK if available, else REST API."""
    if _admin_db:
        try:
            ref = _admin_db.reference(path)
            ref.set(data)
            return True
        except Exception as e:
            logger.warning(f"Admin SDK write failed: {e}")
    return _try_write(path, data)


def _admin_read(path: str) -> dict:
    """Read using Admin SDK if available, else REST API."""
    if _admin_db:
        try:
            ref = _admin_db.reference(path)
            data = ref.get()
            return data if data else {}
        except Exception:
            pass
    return _try_read(path)


def _admin_read_query(path: str, order_by: str, limit: int) -> list:
    """Query using Admin SDK if available, else REST API.
    Falls back to reading all data and sorting locally if query fails.
    """
    if _admin_db:
        try:
            ref = _admin_db.reference(path)
            query = ref.order_by_child(order_by).limit_to_last(limit)
            results = query.get()
            if results:
                if isinstance(results, dict):
                    return list(results.values())
                return results if isinstance(results, list) else []
        except Exception:
            pass
    results = _try_read_query(path, order_by, limit)
    if results:
        return results
    # Fallback: read all data and sort locally (handles missing index)
    all_data = _try_read(path)
    if isinstance(all_data, dict):
        return list(all_data.values())
    if isinstance(all_data, list):
        return all_data
    return []


# ---- Public API (mirrors Android FirebaseManager logic) ----

def submit_global_score(user_id: str, display_name: str, total_score: int, completed_count: int):
    """Submit score to leaderboard (same as Android's FirebaseManager.submitGlobalScore)."""
    entry = {
        "id": user_id,
        "userId": user_id,
        "name": display_name,
        "lessonTitle": f"{completed_count} cours complété(s)",
        "score": total_score,
        "timestamp": int(time.time() * 1000),
    }
    _admin_write(f"leaderboard/{user_id}", entry)


def get_top_scores(limit: int = 10) -> list:
    """Get top N leaderboard entries sorted by score descending."""
    results = _admin_read_query("leaderboard", "score", limit)
    return sorted(results, key=lambda x: x.get("score", 0), reverse=True)[:limit]


def save_progression(user_id: str, session: dict):
    """Save a session to progression (same as Android's FirebaseManager.saveProgression).
    session dict must contain: lessonId, lessonTitleFr, lessonTitleAr, studyDate,
    audioDurationSeconds, audioLanguage, userAnswer, aiScore, aiEvaluation, isSuccess
    """
    entry = {
        "lessonId": session.get("lessonId", ""),
        "lessonTitleFr": session.get("lessonTitleFr", ""),
        "lessonTitleAr": session.get("lessonTitleAr", ""),
        "studyDate": session.get("studyDate", int(time.time() * 1000)),
        "audioDurationSeconds": session.get("audioDurationSeconds", 0),
        "audioLanguage": session.get("audioLanguage", "FR"),
        "userAnswer": session.get("userAnswer", ""),
        "aiScore": session.get("aiScore", 0),
        "aiEvaluation": session.get("aiEvaluation", ""),
        "isSuccess": session.get("isSuccess", False),
    }
    score_id = f"{session.get('lessonId', 'unknown')}_{entry['studyDate']}"
    _admin_write(f"progression/{user_id}/{score_id}", entry)


def get_progression(user_id: str) -> list:
    """Get all progression records for a user (same as Android's fetchAndSyncProgression)."""
    data = _admin_read(f"progression/{user_id}")
    if isinstance(data, dict):
        return list(data.values())
    if isinstance(data, list):
        return data
    return []


def get_unique_guest_number(uid: str) -> int:
    """Generate a consistent guest number from uid (same concept as Android)."""
    return (abs(hash(uid)) % 9000) + 1000


def generate_guest_uid() -> str:
    """Generate a guest user ID."""
    return "guest_" + hashlib.md5(str(time.time()).encode()).hexdigest()[:12]
