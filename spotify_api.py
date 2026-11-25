"""Spotify Web API for deckd: PKCE sign-in (loopback redirect), current track, add to playlist.

Token is kept in %LOCALAPPDATA%\\deckd\\spotify.json (never in the project folder).
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-read-currently-playing user-read-playback-state playlist-read-private playlist-modify-public playlist-modify-private"
STORE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "deckd")
STORE = os.path.join(STORE_DIR, "spotify.json")


class Spotify:
    def __init__(self, client_id, redirect_uri, playlist_id, log):
        self.client_id, self.redirect_uri, self.playlist_id, self.log = client_id, redirect_uri, playlist_id, log
        self.lock = threading.Lock()
        self.pending = {}
        self.playlist = set()     # track ids (and linked_from ids) in the playlist
        self.playlist_at = 0
        self.current = None       # {"id", "name", "artist"}
        self.error = None

    # ---- token storage
    def load(self):
        try:
            with open(STORE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def store(self, d):
        os.makedirs(STORE_DIR, exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, STORE)

    @property
    def signed_in(self):
        return bool(self.load().get("refresh_token"))

    # ---- PKCE login
    def login_url(self):
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        self.pending[state] = verifier
        return AUTH + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
            "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state})

    def callback(self, q):
        verifier = self.pending.pop(q.get("state", ""), None)
        if not verifier:
            return False, "That sign-in link is old. Open /spotify/login again."
        if q.get("error"):
            return False, f"Spotify said: {q['error']}"
        try:
            tok = self._token({"grant_type": "authorization_code", "code": q.get("code", ""),
                               "redirect_uri": self.redirect_uri, "client_id": self.client_id,
                               "code_verifier": verifier})
        except urllib.error.HTTPError as e:
            return False, f"Sign-in failed: {e.code} {e.read()[:200]!r}"
        self.store({"refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
                    "scope": tok.get("scope"), "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60,
                    "authorized_at": time.time()})
        self.error = None
        self.playlist_at = 0
        return True, "Spotify is connected to the deck. You can close this tab."

    def _token(self, form):
        req = urllib.request.Request(TOKEN, data=urllib.parse.urlencode(form).encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def access_token(self):
        with self.lock:
            d = self.load()
            if not d.get("refresh_token"):
                return None
            if d.get("access_token") and time.time() < d.get("expires_at", 0):
                return d["access_token"]
            try:
                tok = self._token({"grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                                   "client_id": self.client_id})
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    d["refresh_token"] = None
                    self.store(d)
                    self.error = "sign-in expired"
                    return None
                raise
            d["access_token"] = tok["access_token"]
            d["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
            if tok.get("refresh_token"):
                d["refresh_token"] = tok["refresh_token"]
            self.store(d)
            return d["access_token"]

    def call(self, method, path, body=None):
        tok = self.access_token()
        if not tok:
            raise PermissionError("not signed in")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    # ---- data
    def refresh_current(self):
        d = self.call("GET", "/me/player/currently-playing?market=from_token")
        item = (d or {}).get("item")
        if not item or item.get("type") != "track":
            self.current = None
            return None
        ids = {item["id"]}
        if item.get("linked_from", {}).get("id"):
            ids.add(item["linked_from"]["id"])
        self.current = {"id": item["id"], "ids": ids, "name": item["name"],
                        "artist": ", ".join(a["name"] for a in item.get("artists", []))}
        return self.current

    def refresh_playlist(self, force=False):
        if not force and time.time() - self.playlist_at < 600:
            return
        ids, path = set(), f"/playlists/{self.playlist_id}/items?limit=100&fields=next,items(item(id,linked_from(id)),track(id,linked_from(id)))"
        while path:
            d = self.call("GET", path)
            for it in d.get("items", []):
                t = it.get("item") or it.get("track") or {}
                if t.get("id"):
                    ids.add(t["id"])
                if (t.get("linked_from") or {}).get("id"):
                    ids.add(t["linked_from"]["id"])
            nxt = d.get("next")
            path = nxt[len(API):] if nxt else None
        self.playlist, self.playlist_at = ids, time.time()

    def in_playlist(self):
        return bool(self.current and self.current["ids"] & self.playlist)

    def add_current(self):
        """Returns (status, text): status in added / already / nothing / error."""
        cur = self.refresh_current()
        if not cur:
            return "nothing", "Nothing playing"
        self.refresh_playlist()
        if cur["ids"] & self.playlist:
            return "already", "Already liked"
        self.call("POST", f"/playlists/{self.playlist_id}/items", {"uris": [f"spotify:track:{cur['id']}"]})
        self.playlist |= cur["ids"]
        return "added", "Added"
