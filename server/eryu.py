#!/usr/bin/env python3
"""eryu — standalone music server for Netease Cloud Music.

Zero external dependencies (Python stdlib only). Handles:
  - Song search, audio URL resolution with CDN fallback, audio streaming
  - Lyrics with translation caching (.lrc + .tlyric)
  - Playlist CRUD (single default + multi-playlist system)
  - Recent play history
  - Music profile (avatar, signature, background)
  - Daily recommendations (based on liked songs)
  - Song memory / notes system
  - Listening stats
  - Roam mode (random genre discovery)
  - Similar song discovery
  - Remote play (push a song to another client)
  - Background audio analysis (via analyze_song.py subprocess)
  - Listen-complete tracking (together count)
  - Static file serving for cached mp3s and frontend

Usage:
    python3 server/eryu.py                     # port 9090
    PORT=8080 python3 server/eryu.py           # custom port

Data layout:
    ./data/music_cache/    — cached mp3, lrc, tlyric, analysis files
    ./data/music_data.json — playlists, recent, profile
    ./data/music_memory.json — per-song memory (notes, listen counts)
    ./data/music_playlist.json — legacy flat playlist (synced with liked)
    ./data/music_remote.json — ephemeral remote-play payload
    ./.secret              — auto-generated auth token
    ./.netease_cred        — MUSIC_U=<cookie> (one line)
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import random
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.request
import urllib.error

HERE = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("eryu")


# ── Secret management ────────────────────────────────────────────────────────

def _load_or_create_secret() -> str:
    secret_file = HERE / ".secret"
    try:
        if secret_file.exists():
            s = secret_file.read_text().strip()
            if s:
                return s
        new_secret = secrets.token_hex(32)
        secret_file.write_text(new_secret)
        secret_file.chmod(0o600)
        logger.info("Auto-generated shared secret saved to %s", secret_file)
        return new_secret
    except Exception as e:
        logger.warning("Could not auto-generate secret: %s", e)
        return ""


# ── Request handler ──────────────────────────────────────────────────────────

class EryuHandler(BaseHTTPRequestHandler):
    state: "ServerState"

    server_version = "Eryu/1.0"

    def log_message(self, fmt, *args):
        logger.info("%s %s", self.address_string(), fmt % args)

    # ── Helpers ──

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _check_auth(self) -> bool:
        if not self.state.shared_secret:
            return True
        token = self.headers.get("X-Auth-Token", "") or self.headers.get("X-Auth", "")
        if not token:
            qs = parse_qs(urlparse(self.path).query)
            token = (qs.get("token") or [""])[0]
        return token == self.state.shared_secret

    def _require_auth(self) -> bool:
        if self._check_auth():
            return True
        self._send_json(403, {"error": "auth required"})
        return False

    def _send_json(self, status: int, body: dict[str, Any]):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, X-Auth")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, file_path: Path, content_type: str | None = None):
        """Serve a file with proper headers and Range support."""
        if not file_path.exists() or not file_path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        size = file_path.stat().st_size
        if content_type is None:
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

        # Range request support (needed for audio seeking)
        range_header = self.headers.get("Range")
        if range_header:
            try:
                range_spec = range_header.replace("bytes=", "")
                start_str, end_str = range_spec.split("-", 1)
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except Exception:
                pass  # Fall through to full response

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, X-Auth")
        self.end_headers()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, X-Auth, Range")
        self.end_headers()

    # ── Netease helpers ──

    def _netease_cookie(self) -> str:
        # Priority 1: environment variable
        env_val = os.environ.get("MUSIC_U", "")
        if env_val:
            return f"MUSIC_U={env_val}"
        # Priority 2: file
        cred = HERE / ".netease_cred"
        try:
            for line in cred.read_text().splitlines():
                if line.startswith("MUSIC_U="):
                    return f"MUSIC_U={line.split('=', 1)[1].strip()}"
        except OSError:
            pass
        return ""

    def _netease_request(self, url: str, data: bytes | None = None,
                         extra_headers: dict[str, str] | None = None,
                         timeout: int = 10) -> Any:
        """Make an authenticated request to Netease API and return parsed JSON."""
        headers = {
            "Cookie": self._netease_cookie(),
            "Referer": "https://music.163.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if extra_headers:
            headers.update(extra_headers)
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _ensure_cover(self, song_id, cover: str = "") -> str:
        if cover:
            return cover
        try:
            url = f"https://music.163.com/api/song/detail?ids=[{song_id}]"
            d = self._netease_request(url)
            return d.get("songs", [{}])[0].get("album", {}).get("picUrl", "")
        except Exception:
            return ""

    # ── Data helpers ──

    def _playlist_path(self) -> Path:
        return self.state.data_dir / "music_playlist.json"

    def _load_playlist(self) -> list:
        p = self._playlist_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return []

    def _save_playlist(self, songs: list):
        p = self._playlist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(songs, ensure_ascii=False))

    def _music_data_path(self) -> Path:
        return self.state.data_dir / "music_data.json"

    def _load_music_data(self) -> dict:
        p = self._music_data_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        # Bootstrap from legacy playlist
        old = self._load_playlist()
        data = {
            "playlists": [{"id": "liked", "name": "Liked", "songs": old}],
            "recent": [],
            "profile": {"avatar": "", "signature": "", "bg": ""},
        }
        self._save_music_data(data)
        return data

    def _save_music_data(self, data: dict):
        p = self._music_data_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1))

    def _song_memory_path(self) -> Path:
        return self.state.data_dir / "music_memory.json"

    def _load_song_memory(self) -> dict:
        p = self._song_memory_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return {}

    def _save_song_memory(self, mem: dict):
        p = self._song_memory_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(mem, ensure_ascii=False, indent=1))

    # ── Audio download with CDN fallback ──

    def _download_audio(self, audio_url: str, cache_file: Path):
        """Download audio to cache_file with CDN fallback for overseas servers."""
        def _dl(dl_url: str):
            areq = urllib.request.Request(dl_url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://music.163.com",
                "Cookie": self._netease_cookie(),
            })
            tmp = cache_file.with_suffix(".tmp")
            with urllib.request.urlopen(areq, timeout=120) as aresp:
                with open(tmp, "wb") as f:
                    while True:
                        chunk = aresp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
            tmp.rename(cache_file)

        try:
            _dl(audio_url)
        except urllib.error.HTTPError:
            # CDN fallback: m*.music.126.net -> m701.music.126.net
            fallback = re.sub(r'm\d+\.music\.126\.net', 'm701.music.126.net', audio_url)
            _dl(fallback)

    def _fetch_music_url(self, song_id) -> bool:
        """Ensure audio is cached, return True if available."""
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.mp3"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            return True
        try:
            url = f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=128000"
            raw = self._netease_request(url)
            audio_url = (raw.get("data") or [{}])[0].get("url")
            if not audio_url:
                return False
            self._download_audio(audio_url, cache_file)
            return cache_file.exists() and cache_file.stat().st_size > 1000
        except Exception:
            return False

    # ── GET routes ────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Health check (no auth)
        if path == "/health":
            self._send_json(200, {"ok": True, "version": "1.0", "service": "eryu"})
            return

        # Static: cached music files (no auth — URLs are unguessable song IDs)
        if path.startswith("/music/file/"):
            self._serve_music_file(path)
            return

        # Static: frontend files from ../client/
        if path == "/" or not path.startswith("/music") and not path.startswith("/health"):
            # Serve frontend static files
            self._serve_static(path)
            return

        # All /music/* endpoints below require auth
        if not self._require_auth():
            return

        if path == "/music/search":
            self._handle_music_search()
        elif path == "/music/url":
            self._handle_music_url()
        elif path == "/music/stream":
            self._handle_music_stream()
        elif path == "/music/lyric":
            self._handle_music_lyric()
        elif path == "/music/playlist":
            self._handle_music_playlist_get()
        elif path == "/music/playlists":
            self._handle_music_playlists_list()
        elif path == "/music/playlists/songs":
            self._handle_music_playlists_songs()
        elif path == "/music/recent":
            self._handle_music_recent_get()
        elif path == "/music/profile":
            self._handle_music_profile_get()
        elif path == "/music/daily":
            self._handle_music_daily()
        elif path == "/music/memory":
            self._handle_music_memory_get()
        elif path == "/music/stats":
            self._handle_music_stats()
        elif path == "/music/roam":
            self._handle_music_roam()
        elif path == "/music/similar":
            self._handle_music_similar()
        elif path == "/music/remote":
            self._handle_music_remote_get()
        elif path == "/music/analyze/status":
            self._handle_analyze_status()
        else:
            self._send_json(404, {"error": "not found"})

    # ── POST routes ───────────────────────────────────────────────────────────

    def do_POST(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/music/playlist/add":
            self._handle_music_playlist_add(body)
        elif path == "/music/playlist/remove":
            self._handle_music_playlist_remove(body)
        elif path == "/music/playlists/create":
            self._handle_music_playlists_create(body)
        elif path == "/music/playlists/rename":
            self._handle_music_playlists_rename(body)
        elif path == "/music/playlists/delete":
            self._handle_music_playlists_delete(body)
        elif path == "/music/playlists/add-song":
            self._handle_music_playlists_add_song(body)
        elif path == "/music/playlists/remove-song":
            self._handle_music_playlists_remove_song(body)
        elif path == "/music/recent/add":
            self._handle_music_recent_add(body)
        elif path == "/music/memory":
            self._handle_music_memory_save(body)
        elif path == "/music/analyze":
            self._handle_analyze_trigger(body)
        elif path == "/music/listen-together":
            self._handle_listen_together(body)
        elif path == "/music/listen-complete":
            self._handle_music_listen_complete(body)
        elif path == "/music/profile":
            self._handle_music_profile_update(body)
        elif path == "/music/remote":
            self._handle_music_remote_post(body)
        else:
            self._send_json(404, {"error": "not found"})

    # ── Static file serving ───────────────────────────────────────────────────

    def _serve_music_file(self, path: str):
        """Serve cached mp3/png files from data/music_cache/."""
        filename = path[len("/music/file/"):]
        if not filename or ".." in filename or "/" in filename:
            self._send_json(400, {"error": "bad path"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        target = (cache_dir / filename).resolve()
        # Path traversal guard
        try:
            target.relative_to(cache_dir.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        self._send_file(target)

    def _serve_static(self, path: str):
        """Serve frontend static files from ../client/ directory."""
        client_dir = HERE.parent / "client"
        if not client_dir.is_dir():
            self._send_json(404, {"error": "frontend not found — place files in ../client/"})
            return
        rel = path.lstrip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel:
            self._send_json(403, {"error": "forbidden"})
            return
        target = (client_dir / rel).resolve()
        try:
            target.relative_to(client_dir.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        # SPA fallback: if file not found, serve index.html
        if not target.exists() or not target.is_file():
            target = client_dir / "index.html"
            if not target.exists():
                self._send_json(404, {"error": "not found"})
                return
        self._send_file(target)

    # ── Music endpoint handlers ───────────────────────────────────────────────

    def _handle_music_search(self):
        qs = parse_qs(urlparse(self.path).query)
        keyword = qs.get("q", [""])[0]
        if not keyword:
            self._send_json(400, {"error": "missing q"})
            return
        try:
            url = "https://music.163.com/api/search/get"
            post_data = urlencode({
                "s": keyword, "type": "1", "limit": "6", "offset": "0"
            }).encode()
            raw = self._netease_request(url, data=post_data)
            songs = []
            result = raw.get("result", {})
            if not isinstance(result, dict):
                self._send_json(200, {"ok": True, "songs": []})
                return
            raw_songs = result.get("songs", [])[:6]
            # Batch-fetch covers
            ids = [s.get("id") for s in raw_songs if s.get("id")]
            covers: dict[int, str] = {}
            if ids:
                try:
                    detail_url = f"https://music.163.com/api/song/detail?ids=[{','.join(str(i) for i in ids)}]"
                    detail = self._netease_request(detail_url)
                    for ds in detail.get("songs", []):
                        al = ds.get("album", {}) or {}
                        if al.get("picUrl"):
                            covers[ds.get("id")] = al["picUrl"]
                except Exception:
                    pass
            for s in raw_songs:
                artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                album = s.get("album", {}) or {}
                cover = covers.get(s.get("id"), album.get("picUrl", "") or "")
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover
                songs.append({
                    "id": s.get("id"),
                    "name": s.get("name", ""),
                    "artist": artists,
                    "album": album.get("name", ""),
                    "cover": cover,
                })
            self._send_json(200, {"ok": True, "songs": songs})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_music_url(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.mp3"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            self._send_json(200, {"ok": True, "url": f"/music/file/{song_id}.mp3", "cached": True})
            return
        try:
            url = f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=128000"
            raw = self._netease_request(url)
            data_list = raw.get("data", [])
            audio_url = data_list[0].get("url") if data_list else None
            if not audio_url:
                self._send_json(200, {"ok": False, "error": "no url, may need VIP or song unavailable"})
                return
            self._download_audio(audio_url, cache_file)
            self._send_json(200, {"ok": True, "url": f"/music/file/{song_id}.mp3", "cached": True})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_music_stream(self):
        """Stream audio directly — resolve URL, cache, and redirect to file."""
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.mp3"
        if not (cache_file.exists() and cache_file.stat().st_size > 0):
            # Try to fetch and cache the file
            if not self._fetch_music_url(song_id):
                self._send_json(404, {"ok": False, "error": "audio unavailable"})
                return
        self._send_file(cache_file, "audio/mpeg")

    def _handle_music_lyric(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.lrc"
        cache_trans = cache_dir / f"{song_id}.tlyric"
        # Serve from cache if available
        if cache_file.exists():
            tlyric = cache_trans.read_text() if cache_trans.exists() else ""
            self._send_json(200, {"ok": True, "lrc": cache_file.read_text(), "tlyric": tlyric})
            return
        try:
            url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&tv=-1"
            raw = self._netease_request(url)
            lrc = raw.get("lrc", {}).get("lyric", "")
            tlyric = raw.get("tlyric", {}).get("lyric", "")
            # Cache BOTH .lrc AND .tlyric (critical: both must be saved)
            if lrc:
                cache_file.write_text(lrc)
            if tlyric:
                cache_trans.write_text(tlyric)
            self._send_json(200, {"ok": True, "lrc": lrc, "tlyric": tlyric})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    # ── Playlist (legacy flat) ──

    def _handle_music_playlist_get(self):
        self._send_json(200, {"ok": True, "songs": self._load_playlist()})

    def _handle_music_playlist_add(self, body: dict):
        song = body.get("song")
        if not song or not song.get("songId"):
            self._send_json(400, {"error": "missing song"})
            return
        song["cover"] = self._ensure_cover(song["songId"], song.get("cover", ""))
        song["addedBy"] = body.get("by", "unknown")
        playlist = self._load_playlist()
        if any(s.get("songId") == song["songId"] for s in playlist):
            self._send_json(200, {"ok": True, "duplicate": True, "songs": playlist})
            return
        playlist.append(song)
        self._save_playlist(playlist)
        # Also add to "liked" in multi-playlist system
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == "liked":
                if not any(s.get("songId") == song["songId"] for s in pl["songs"]):
                    pl["songs"].append(song)
                self._save_music_data(data)
                break
        self._send_json(200, {"ok": True, "songs": playlist})

    def _handle_music_playlist_remove(self, body: dict):
        song_id = body.get("songId")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        playlist = self._load_playlist()
        playlist = [s for s in playlist if s.get("songId") != song_id]
        self._save_playlist(playlist)
        self._send_json(200, {"ok": True, "songs": playlist})

    # ── Multi-playlist system ──

    def _handle_music_playlists_list(self):
        data = self._load_music_data()
        out = []
        for pl in data["playlists"]:
            cover = ""
            if pl["songs"]:
                cover = pl["songs"][0].get("cover", "")
            out.append({"id": pl["id"], "name": pl["name"], "count": len(pl["songs"]), "cover": cover})
        self._send_json(200, {"ok": True, "playlists": out})

    def _handle_music_playlists_songs(self):
        qs = parse_qs(urlparse(self.path).query)
        pid = qs.get("id", [""])[0]
        if not pid:
            self._send_json(400, {"error": "missing id"})
            return
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                self._send_json(200, {"ok": True, "songs": pl["songs"]})
                return
        self._send_json(404, {"error": "not found"})

    def _handle_music_playlists_create(self, body: dict):
        name = body.get("name", "").strip()
        if not name:
            self._send_json(400, {"error": "missing name"})
            return
        data = self._load_music_data()
        pl = {"id": uuid.uuid4().hex[:8], "name": name, "songs": []}
        data["playlists"].append(pl)
        self._save_music_data(data)
        self._send_json(200, {"ok": True, "playlist": {"id": pl["id"], "name": pl["name"], "count": 0, "cover": ""}})

    def _handle_music_playlists_rename(self, body: dict):
        pid = body.get("id", "")
        name = body.get("name", "").strip()
        if not pid or not name:
            self._send_json(400, {"error": "missing id or name"})
            return
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                pl["name"] = name
                self._save_music_data(data)
                self._send_json(200, {"ok": True})
                return
        self._send_json(404, {"error": "not found"})

    def _handle_music_playlists_delete(self, body: dict):
        pid = body.get("id", "")
        if not pid or pid == "liked":
            self._send_json(400, {"error": "cannot delete"})
            return
        data = self._load_music_data()
        data["playlists"] = [p for p in data["playlists"] if p["id"] != pid]
        self._save_music_data(data)
        self._send_json(200, {"ok": True})

    def _handle_music_playlists_add_song(self, body: dict):
        pid = body.get("playlistId", "")
        song = body.get("song")
        if not pid or not song or not song.get("songId"):
            self._send_json(400, {"error": "missing playlistId or song"})
            return
        song["cover"] = self._ensure_cover(song["songId"], song.get("cover", ""))
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                if any(s.get("songId") == song["songId"] for s in pl["songs"]):
                    self._send_json(200, {"ok": True, "duplicate": True})
                    return
                song["addedBy"] = body.get("by", "unknown")
                pl["songs"].append(song)
                self._save_music_data(data)
                if pid == "liked":
                    self._save_playlist(pl["songs"])
                self._send_json(200, {"ok": True})
                return
        self._send_json(404, {"error": "playlist not found"})

    def _handle_music_playlists_remove_song(self, body: dict):
        pid = body.get("playlistId", "")
        song_id = body.get("songId")
        if not pid or not song_id:
            self._send_json(400, {"error": "missing playlistId or songId"})
            return
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                pl["songs"] = [s for s in pl["songs"] if s.get("songId") != song_id]
                self._save_music_data(data)
                if pid == "liked":
                    self._save_playlist(pl["songs"])
                self._send_json(200, {"ok": True})
                return
        self._send_json(404, {"error": "playlist not found"})

    # ── Recent play history ──

    def _handle_music_recent_get(self):
        data = self._load_music_data()
        self._send_json(200, {"ok": True, "songs": data.get("recent", [])[:30]})

    def _handle_music_recent_add(self, body: dict):
        song = body.get("song")
        if not song or not song.get("songId"):
            self._send_json(200, {"ok": True})
            return
        data = self._load_music_data()
        recent = data.get("recent", [])
        recent = [s for s in recent if s.get("songId") != song["songId"]]
        song["playedAt"] = datetime.now(timezone.utc).isoformat()
        recent.insert(0, song)
        data["recent"] = recent[:50]
        self._save_music_data(data)
        # Auto-increment listen count in song memory
        mem = self._load_song_memory()
        sid = str(song["songId"])
        entry = mem.get(sid, {
            "songId": song["songId"],
            "name": song.get("name", ""),
            "artist": song.get("artist", ""),
            "listenCount": 0,
            "togetherCount": 0,
            "firstListened": None,
            "lastListened": None,
            "analyzed": False,
            "notes": "",
            "feeling": "",
            "favoriteLines": [],
            "tags": [],
        })
        entry["listenCount"] = entry.get("listenCount", 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        entry["lastListened"] = now
        if not entry.get("firstListened"):
            entry["firstListened"] = now
        entry["name"] = song.get("name", entry.get("name", ""))
        entry["artist"] = song.get("artist", entry.get("artist", ""))
        mem[sid] = entry
        self._save_song_memory(mem)
        self._send_json(200, {"ok": True})

    # ── Song memory system ──

    def _handle_music_memory_get(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        mem = self._load_song_memory()
        if song_id:
            entry = mem.get(str(song_id))
            self._send_json(200, {"ok": True, "memory": entry})
        else:
            self._send_json(200, {"ok": True, "memories": mem})

    def _handle_music_memory_save(self, body: dict):
        song_id = str(body.get("songId", ""))
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        mem = self._load_song_memory()
        entry = mem.get(song_id, {
            "songId": int(song_id),
            "name": "",
            "artist": "",
            "listenCount": 0,
            "togetherCount": 0,
            "firstListened": None,
            "lastListened": None,
            "analyzed": False,
            "notes": "",
            "feeling": "",
            "favoriteLines": [],
            "tags": [],
        })
        now = datetime.now(timezone.utc).isoformat()
        action = body.get("action", "listen")
        if action == "listen":
            entry["listenCount"] = entry.get("listenCount", 0) + 1
            entry["lastListened"] = now
            if not entry.get("firstListened"):
                entry["firstListened"] = now
            entry["name"] = body.get("name", entry.get("name", ""))
            entry["artist"] = body.get("artist", entry.get("artist", ""))
        elif action == "together":
            entry["togetherCount"] = entry.get("togetherCount", 0) + 1
            entry["lastListened"] = now
        elif action == "analyze":
            entry["analyzed"] = True
            if body.get("notes"):
                entry["notes"] = body["notes"]
            if body.get("feeling"):
                entry["feeling"] = body["feeling"]
            if body.get("favoriteLines"):
                entry["favoriteLines"] = body["favoriteLines"]
            if body.get("tags"):
                entry["tags"] = body["tags"]
            if body.get("bpm"):
                entry["bpm"] = body["bpm"]
            if body.get("duration"):
                entry["duration"] = body["duration"]
        elif action == "like":
            entry["liked"] = True
            entry["name"] = body.get("name", entry.get("name", ""))
            entry["artist"] = body.get("artist", entry.get("artist", ""))
            cover = self._ensure_cover(song_id, body.get("cover", ""))
            song_obj = {
                "songId": int(song_id),
                "name": entry["name"],
                "artist": entry["artist"],
                "cover": cover,
                "addedBy": body.get("by", "user"),
            }
            data = self._load_music_data()
            # Add to a "Liked by User" playlist (auto-create if missing)
            liked_pl = None
            for pl in data.get("playlists", []):
                if pl.get("id") == "user_liked":
                    liked_pl = pl
                    break
            if not liked_pl:
                liked_pl = {"id": "user_liked", "name": "User Liked", "songs": []}
                data.setdefault("playlists", []).append(liked_pl)
            if not any(s.get("songId") == int(song_id) for s in liked_pl["songs"]):
                liked_pl["songs"].append(song_obj)
                self._save_music_data(data)
        elif action == "note":
            entry["notes"] = body.get("notes", entry.get("notes", ""))
            if body.get("feeling"):
                entry["feeling"] = body["feeling"]
            if body.get("favoriteLines"):
                entry["favoriteLines"] = body["favoriteLines"]
        mem[song_id] = entry
        self._save_song_memory(mem)
        self._send_json(200, {"ok": True, "memory": entry})

    # ── Listen together ──

    def _handle_listen_together(self, body: dict):
        """Record a 'listen together' event. In standalone mode this just logs
        the event; in the full CcCompanion it also injects into tmux."""
        song_id = body.get("songId")
        name = body.get("name", "")
        artist = body.get("artist", "")
        cover = self._ensure_cover(song_id, body.get("cover", ""))
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        is_roam = body.get("roam", False)
        # Record in song memory
        mem = self._load_song_memory()
        sid = str(song_id)
        entry = mem.get(sid, {
            "songId": song_id,
            "name": name,
            "artist": artist,
            "listenCount": 0,
            "togetherCount": 0,
            "firstListened": None,
            "lastListened": None,
            "analyzed": False,
            "notes": "",
            "feeling": "",
            "favoriteLines": [],
            "tags": [],
        })
        now = datetime.now(timezone.utc).isoformat()
        entry["listenCount"] = entry.get("listenCount", 0) + 1
        entry["lastListened"] = now
        if not entry.get("firstListened"):
            entry["firstListened"] = now
        entry["name"] = name or entry.get("name", "")
        entry["artist"] = artist or entry.get("artist", "")
        mem[sid] = entry
        self._save_song_memory(mem)
        logger.info("listen-together: %s — %s (roam=%s)", name, artist, is_roam)
        self._send_json(200, {"ok": True})

    def _handle_music_listen_complete(self, body: dict):
        """Called when a song finishes playing naturally (audio ended event)."""
        song_id = body.get("songId")
        source = body.get("source", "")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        if source != "together":
            self._send_json(200, {"ok": True, "counted": False})
            return
        sid = str(song_id)
        mem = self._load_song_memory()
        entry = mem.get(sid)
        if not entry:
            self._send_json(200, {"ok": True, "counted": False})
            return
        now = datetime.now(timezone.utc).isoformat()
        entry["togetherCount"] = entry.get("togetherCount", 0) + 1
        entry["lastListened"] = now
        if not entry.get("firstListened"):
            entry["firstListened"] = now
        mem[sid] = entry
        self._save_song_memory(mem)
        self._send_json(200, {"ok": True, "counted": True})

    # ── Background pre-analysis ──

    def _handle_analyze_trigger(self, body: dict):
        song_id = body.get("songId")
        song_name = body.get("name", "")
        song_artist = body.get("artist", "")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        result_file = cache_dir / f"{song_id}_preanalysis.json"
        marker_file = cache_dir / f"{song_id}.analyzing"
        if result_file.exists():
            self._send_json(200, {"ok": True, "status": "ready"})
            return
        if marker_file.exists():
            age = time.time() - marker_file.stat().st_mtime
            if age < 60:
                self._send_json(200, {"ok": True, "status": "running"})
                return
            marker_file.unlink(missing_ok=True)
        audio_file = cache_dir / f"{song_id}.mp3"
        if not audio_file.exists():
            if not self._fetch_music_url(song_id):
                self._send_json(400, {"error": "cannot fetch audio"})
                return
        marker_file.write_text(json.dumps({
            "songId": song_id, "name": song_name, "started": time.time()
        }))
        script = str(HERE / "analyze_song.py")
        subprocess.Popen(
            ["python3", script, str(song_id), song_name, song_artist, str(cache_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._send_json(200, {"ok": True, "status": "started"})

    def _handle_analyze_status(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        result_file = cache_dir / f"{song_id}_preanalysis.json"
        if result_file.exists():
            result = json.loads(result_file.read_text())
            self._send_json(200, {"ok": True, "status": "ready", "analysis": result})
            return
        marker_file = cache_dir / f"{song_id}.analyzing"
        if marker_file.exists():
            age = time.time() - marker_file.stat().st_mtime
            if age < 60:
                self._send_json(200, {"ok": True, "status": "running"})
                return
            marker_file.unlink(missing_ok=True)
        err_file = cache_dir / f"{song_id}_analyze_error.txt"
        if err_file.exists():
            err = err_file.read_text()
            self._send_json(200, {"ok": True, "status": f"error: {err}"})
            return
        self._send_json(200, {"ok": True, "status": "none"})

    # ── Stats ──

    def _handle_music_stats(self):
        mem = self._load_song_memory()
        total_songs = len(mem)
        total_listens = sum(e.get("listenCount", 0) for e in mem.values())
        together_listens = sum(e.get("togetherCount", 0) for e in mem.values())
        analyzed = sum(1 for e in mem.values() if e.get("analyzed"))
        top = sorted(mem.values(), key=lambda e: e.get("listenCount", 0), reverse=True)[:10]
        top_list = [
            {"name": e.get("name", ""), "artist": e.get("artist", ""),
             "count": e.get("listenCount", 0), "songId": e.get("songId")}
            for e in top
        ]
        self._send_json(200, {"ok": True, "stats": {
            "totalSongs": total_songs,
            "totalListens": total_listens,
            "togetherListens": together_listens,
            "analyzedSongs": analyzed,
            "topSongs": top_list,
        }})

    # ── Profile ──

    def _handle_music_profile_get(self):
        data = self._load_music_data()
        self._send_json(200, {"ok": True, "profile": data.get("profile", {})})

    def _handle_music_profile_update(self, body: dict):
        data = self._load_music_data()
        profile = data.get("profile", {})
        for k in ("avatar", "signature", "bg", "name", "appBg"):
            if k in body:
                profile[k] = body[k]
        data["profile"] = profile
        self._save_music_data(data)
        self._send_json(200, {"ok": True, "profile": profile})

    # ── Daily recommendations ──

    def _handle_music_daily(self):
        data = self._load_music_data()
        liked = []
        for pl in data["playlists"]:
            if pl["id"] == "liked":
                liked = pl["songs"]
                break
        if not liked:
            self._send_json(200, {"ok": True, "songs": []})
            return
        seed_song = random.choice(liked)
        if not seed_song.get("songId"):
            self._send_json(200, {"ok": True, "songs": []})
            return
        try:
            url = f"https://music.163.com/api/discovery/simiSong?songid={seed_song['songId']}&offset=0&limit=6"
            raw = self._netease_request(url)
            songs = []
            for s in raw.get("songs", [])[:6]:
                artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                al = s.get("album", {}) or {}
                cover = al.get("picUrl", "")
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover
                songs.append({
                    "id": s["id"], "name": s.get("name", ""), "artist": artists,
                    "album": al.get("name", ""), "cover": cover,
                })
            self._send_json(200, {"ok": True, "songs": songs, "seed": seed_song.get("name", "")})
        except Exception as e:
            self._send_json(200, {"ok": True, "songs": [], "error": str(e)})

    # ── Remote play ──

    def _handle_music_remote_get(self):
        f = self.state.data_dir / "music_remote.json"
        if f.exists():
            data = json.loads(f.read_text())
            f.unlink()
            self._send_json(200, {"ok": True, "song": data})
        else:
            self._send_json(200, {"ok": False})

    def _handle_music_remote_post(self, body: dict):
        song = body.get("song")
        if not song:
            self._send_json(400, {"error": "missing song"})
            return
        f = self.state.data_dir / "music_remote.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(song, ensure_ascii=False))
        self._send_json(200, {"ok": True})

    # ── Roam mode (random genre discovery) ──

    def _handle_music_roam(self):
        """Diverse random song discovery — rotates across genres/languages."""
        # Netease top song area IDs: 0=All, 7=Chinese, 96=Western, 8=Japanese, 16=Korean
        # Netease playlist IDs for genre diversity
        genre_playlists = [
            3779629,      # Chinese classics
            2884035,      # Western classics
            71384707,     # Japanese pop
            991319590,    # Korean pop
            60198,        # Hip-hop/Rap
            11640012,     # R&B
            5059642708,   # Electronic
            2529283982,   # Folk
            3136952023,   # Rock
        ]
        top_types = [0, 7, 96, 8, 16]
        strategy = random.choice(["top", "playlist"])
        try:
            songs = []
            if strategy == "top":
                t = random.choice(top_types)
                url = f"https://music.163.com/api/discovery/new/songs?areaId={t}&limit=50&total=true"
                raw = self._netease_request(url)
                for s in raw.get("data", []):
                    artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                    al = s.get("album", {}) or {}
                    cover = al.get("picUrl", "")
                    if cover and not cover.startswith("http"):
                        cover = "https:" + cover
                    songs.append({
                        "songId": s["id"], "name": s.get("name", ""), "artist": artists,
                        "album": al.get("name", ""), "cover": cover,
                    })
            else:
                pid = random.choice(genre_playlists)
                url = f"https://music.163.com/api/playlist/detail?id={pid}"
                raw = self._netease_request(url)
                result = raw.get("result", {})
                for s in result.get("tracks", []):
                    artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                    al = s.get("album", {}) or {}
                    cover = al.get("picUrl", "")
                    if cover and not cover.startswith("http"):
                        cover = "https:" + cover
                    songs.append({
                        "songId": s["id"], "name": s.get("name", ""), "artist": artists,
                        "album": al.get("name", ""), "cover": cover,
                    })
            if songs:
                pick = random.choice(songs)
                self._send_json(200, {"ok": True, "song": pick})
            else:
                self._send_json(200, {"ok": False, "error": "no songs found"})
        except Exception as e:
            self._send_json(200, {"ok": False, "error": str(e)})

    # ── Similar songs ──

    def _handle_music_similar(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        try:
            url = f"https://music.163.com/api/discovery/simiSong?songid={song_id}&offset=0&total=true&limit=6"
            raw = self._netease_request(url)
            raw_songs = raw.get("songs", [])[:6]
            songs = []
            for s in raw_songs:
                artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                album = s.get("album", {}) or {}
                cover = album.get("picUrl", "") or ""
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover
                songs.append({
                    "id": s.get("id"),
                    "name": s.get("name", ""),
                    "artist": artists,
                    "album": album.get("name", ""),
                    "cover": cover,
                })
            self._send_json(200, {"ok": True, "songs": songs})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})


# ── Server state ─────────────────────────────────────────────────────────────

class ServerState:
    def __init__(self, port: int):
        self.host = "0.0.0.0"
        self.port = port
        self.shared_secret = _load_or_create_secret()
        self.data_dir = HERE / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "music_cache").mkdir(parents=True, exist_ok=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    port = int(os.environ.get("PORT", "9090"))
    state = ServerState(port)
    EryuHandler.state = state

    server = ThreadingHTTPServer((state.host, state.port), EryuHandler)
    logger.info("eryu starting on %s:%d", state.host, state.port)
    logger.info("Data dir: %s", state.data_dir)
    if state.shared_secret:
        logger.info("Auth token: %s", state.shared_secret[:8] + "...")
        logger.info("(Full token in %s)", HERE / ".secret")
    else:
        logger.warning("No shared secret — all requests allowed!")
    logger.info("Netease cookie: %s", "configured" if (HERE / ".netease_cred").exists() else "NOT FOUND — create .netease_cred with MUSIC_U=<value>")
    logger.info("Frontend: %s", "found" if (HERE.parent / "client").is_dir() else "not found (place files in ../client/)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
