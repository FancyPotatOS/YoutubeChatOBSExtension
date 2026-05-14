#!/usr/bin/env python3
"""Small YouTube Data API helper for live chat bot commands.

This file intentionally uses only the Python standard library so the watcher can
keep running without installing Google client packages.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


API_BASE_URL = "https://www.googleapis.com/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

DEFAULT_CLIENT_SECRETS_FILE = Path("client_secret.json")
DEFAULT_TOKEN_FILE = Path("youtube_oauth_token.json")


class YouTubeAuthError(RuntimeError):
    """Raised when OAuth credentials are missing, invalid, or denied."""


class YouTubeApiError(RuntimeError):
    """Raised when a YouTube API request fails."""


def _urlsafe_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _json_load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise YouTubeAuthError(f"Missing file: {path}") from error
    except json.JSONDecodeError as error:
        raise YouTubeAuthError(f"{path} is not valid JSON: {error}") from error


def _json_save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _load_client_config(path: Path) -> dict:
    data = _json_load(path)
    config = data.get("installed") or data.get("web")
    if not isinstance(config, dict):
        raise YouTubeAuthError(
            f"{path} must be a Google OAuth client JSON file with an "
            "'installed' or 'web' section."
        )

    if not config.get("client_id"):
        raise YouTubeAuthError(f"{path} does not contain a client_id.")

    return config


def _form_post(url: str, form_data: dict[str, str]) -> dict:
    encoded = urllib.parse.urlencode(form_data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise YouTubeAuthError(_format_http_error(error)) from error


def _format_http_error(error: urllib.error.HTTPError) -> str:
    raw_body = error.read().decode("utf-8", errors="replace")
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        body = raw_body

    if isinstance(body, dict):
        api_error = body.get("error")
        if isinstance(api_error, dict):
            message = api_error.get("message") or json.dumps(api_error)
        else:
            message = body.get("error_description") or api_error or json.dumps(body)
    else:
        message = body

    return f"HTTP {error.code}: {message}"


class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.oauth_params = params  # type: ignore[attr-defined]

        if "error" in params:
            body = "YouTube OAuth was denied. You can close this tab."
        else:
            body = "YouTube OAuth complete. You can close this tab."

        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def authorize(
    client_secrets_file: Path = DEFAULT_CLIENT_SECRETS_FILE,
    token_file: Path = DEFAULT_TOKEN_FILE,
    *,
    open_browser: bool = True,
) -> dict:
    """Run the installed-app OAuth flow and persist the resulting token."""
    client_config = _load_client_config(client_secrets_file)
    code_verifier = _urlsafe_b64(secrets.token_bytes(64))
    code_challenge = _urlsafe_b64(hashlib.sha256(code_verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(32)

    server = http.server.HTTPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    server.timeout = 300
    server.oauth_params = {}  # type: ignore[attr-defined]
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"

    auth_params = {
        "client_id": client_config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": YOUTUBE_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    print("Opening Google OAuth consent in your browser.", flush=True)
    print("If it does not open, visit this URL:", flush=True)
    print(auth_url, flush=True)
    if open_browser:
        webbrowser.open(auth_url)

    server.handle_request()
    params = server.oauth_params  # type: ignore[attr-defined]
    if not params:
        raise YouTubeAuthError("Timed out waiting for the OAuth redirect.")
    if params.get("state", [""])[0] != state:
        raise YouTubeAuthError("OAuth state mismatch. Refusing to use this response.")
    if "error" in params:
        error = params["error"][0]
        description = params.get("error_description", [""])[0]
        raise YouTubeAuthError(f"OAuth failed: {error} {description}".strip())

    code = params.get("code", [""])[0]
    if not code:
        raise YouTubeAuthError("OAuth redirect did not include an authorization code.")

    token_request = {
        "client_id": client_config["client_id"],
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client_config.get("client_secret"):
        token_request["client_secret"] = client_config["client_secret"]

    token = _form_post(client_config.get("token_uri") or TOKEN_URL, token_request)
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 0))
    _json_save(token_file, token)
    return token


def extract_video_id_from_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("v"):
        return query["v"][0]

    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    if host.endswith("youtu.be") and path_parts:
        return path_parts[0]
    if len(path_parts) >= 2 and path_parts[0] in {"live", "shorts", "embed"}:
        return path_parts[1]
    return None


def extract_live_chat_message_id(chat_item: dict) -> str | None:
    message_id = str(chat_item.get("id") or "").strip()
    for prefix in ("message-", "chat-message-"):
        if message_id.startswith(prefix):
            message_id = message_id[len(prefix) :]

    return message_id or None


class YouTubeApi:
    def __init__(
        self,
        client_secrets_file: Path | str | None = None,
        token_file: Path | str | None = None,
    ) -> None:
        self.client_secrets_file = Path(
            client_secrets_file
            or os.environ.get("YOUTUBE_CLIENT_SECRETS_FILE")
            or DEFAULT_CLIENT_SECRETS_FILE
        )
        self.token_file = Path(
            token_file
            or os.environ.get("YOUTUBE_OAUTH_TOKEN_FILE")
            or DEFAULT_TOKEN_FILE
        )

    def authorize(self) -> dict:
        return authorize(self.client_secrets_file, self.token_file)

    def get_access_token(self) -> str:
        token = self._load_token()
        expires_at = int(token.get("expires_at") or 0)
        if token.get("access_token") and expires_at > int(time.time()) + 60:
            return str(token["access_token"])

        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise YouTubeAuthError(
                f"{self.token_file} has no refresh_token. Run "
                "'python .\\youtube_api.py auth' again."
            )

        client_config = _load_client_config(self.client_secrets_file)
        refresh_request = {
            "client_id": client_config["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": str(refresh_token),
        }
        if client_config.get("client_secret"):
            refresh_request["client_secret"] = client_config["client_secret"]

        refreshed = _form_post(
            client_config.get("token_uri") or TOKEN_URL,
            refresh_request,
        )
        token.update(refreshed)
        token["refresh_token"] = refresh_token
        token["expires_at"] = int(time.time()) + int(refreshed.get("expires_in", 0))
        _json_save(self.token_file, token)
        return str(token["access_token"])

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict | None = None,
        retry_on_unauthorized: bool = True,
    ) -> dict | None:
        url = f"{API_BASE_URL}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        data = None
        headers = {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 204:
                    return None
                raw_body = response.read().decode("utf-8")
                return json.loads(raw_body) if raw_body else None
        except urllib.error.HTTPError as error:
            if error.code == 401 and retry_on_unauthorized:
                token = self._load_token()
                token["expires_at"] = 0
                _json_save(self.token_file, token)
                return self.request(
                    method,
                    path,
                    params=params,
                    body=body,
                    retry_on_unauthorized=False,
                )
            raise YouTubeApiError(_format_http_error(error)) from error

    def get_live_chat_id_for_video(self, video_id: str) -> str:
        response = self.request(
            "GET",
            "/videos",
            params={"part": "liveStreamingDetails", "id": video_id},
        )
        items = (response or {}).get("items") or []
        for item in items:
            live_chat_id = (
                item.get("liveStreamingDetails", {}).get("activeLiveChatId")
            )
            if live_chat_id:
                return str(live_chat_id)

        response = self.request(
            "GET",
            "/liveBroadcasts",
            params={"part": "snippet", "id": video_id},
        )
        items = (response or {}).get("items") or []
        for item in items:
            live_chat_id = item.get("snippet", {}).get("liveChatId")
            if live_chat_id:
                return str(live_chat_id)

        raise YouTubeApiError(f"No active live chat ID found for video {video_id}.")

    def send_live_chat_message(self, live_chat_id: str, text: str) -> dict:
        response = self.request(
            "POST",
            "/liveChat/messages",
            params={"part": "snippet"},
            body={
                "snippet": {
                    "liveChatId": live_chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {"messageText": text},
                }
            },
        )
        return response or {}

    def send_live_chat_message_for_video(self, video_id: str, text: str) -> dict:
        live_chat_id = self.get_live_chat_id_for_video(video_id)
        return self.send_live_chat_message(live_chat_id, text)

    def delete_live_chat_message(self, message_id: str) -> None:
        self.request("DELETE", "/liveChat/messages", params={"id": message_id})

    def _load_token(self) -> dict:
        try:
            return _json_load(self.token_file)
        except YouTubeAuthError as error:
            raise YouTubeAuthError(
                f"{error}. Run 'python .\\youtube_api.py auth' first."
            ) from error


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-secrets",
        default=str(DEFAULT_CLIENT_SECRETS_FILE),
        help="Google OAuth client JSON file.",
    )
    parser.add_argument(
        "--token",
        default=str(DEFAULT_TOKEN_FILE),
        help="Where to read/write the local OAuth token.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("auth", help="Authorize this machine with Google OAuth.")

    chat_id = subparsers.add_parser("chat-id", help="Print the liveChatId for a video.")
    chat_id.add_argument("--video-id", required=True)

    send = subparsers.add_parser("send", help="Send a public message to live chat.")
    send.add_argument("--video-id", required=True)
    send.add_argument("--text", required=True)

    delete = subparsers.add_parser("delete", help="Delete a live chat message by ID.")
    delete.add_argument("--message-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    api = YouTubeApi(args.client_secrets, args.token)

    try:
        if args.command == "auth":
            api.authorize()
            print(f"OAuth token saved to {api.token_file}", flush=True)
        elif args.command == "chat-id":
            print(api.get_live_chat_id_for_video(args.video_id), flush=True)
        elif args.command == "send":
            result = api.send_live_chat_message_for_video(args.video_id, args.text)
            print(json.dumps(result, indent=2), flush=True)
        elif args.command == "delete":
            api.delete_live_chat_message(args.message_id)
            print("Deleted.", flush=True)
        else:
            raise AssertionError(f"Unhandled command: {args.command}")
    except (YouTubeAuthError, YouTubeApiError) as error:
        print(error, file=sys.stderr, flush=True)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
