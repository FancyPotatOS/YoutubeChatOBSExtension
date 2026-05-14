#!/usr/bin/env python3
"""Watch a YouTube live chat tab through Chrome DevTools Protocol.

Chrome must be started with --remote-debugging-port before this script can
attach to a tab. No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable
import inspect
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import stream_commands

BINDING_NAME = "ytChatPythonItem"

WATCHER_JS = r"""
(() => {
  const bindingName = "ytChatPythonItem";
  const chatItemSelectors = [
    "yt-live-chat-text-message-renderer",
    "yt-live-chat-paid-message-renderer",
    "yt-live-chat-paid-sticker-renderer",
    "yt-live-chat-membership-item-renderer"
  ];
  const chatItemSelector = chatItemSelectors.join(",");
  const includeExistingOnStart = __INCLUDE_EXISTING_ON_START__;

  if (typeof window[bindingName] !== "function") {
    return "binding is not available";
  }

  if (window.__ytChatPythonWatcher?.observer) {
    window.__ytChatPythonWatcher.observer.disconnect();
  }

  const instanceId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  const state = {
    observer: null,
    nextItemId: 1,
    seenItems: new WeakSet()
  };
  window.__ytChatPythonWatcher = state;

  const normalizeText = (value) => (value || "").replace(/\s+/g, " ").trim();

  const getText = (root, selector) => {
    const element = root.querySelector(selector);
    return normalizeText(element?.innerText || element?.textContent || "");
  };

  const getImageUrl = (root) => {
    const image = root.querySelector("#img, yt-img-shadow img, img");
    return image?.src || "";
  };

  const getChatItemType = (item) => (
    item.tagName
      .toLowerCase()
      .replace("yt-live-chat-", "")
      .replace("-renderer", "")
  );

  const getWatcherId = (item) => {
    if (!item.dataset.ytChatPythonWatcherId) {
      item.dataset.ytChatPythonWatcherId = `${instanceId}-${state.nextItemId}`;
      state.nextItemId += 1;
    }

    return item.dataset.ytChatPythonWatcherId;
  };

  const readChatItem = (item) => ({
    type: getChatItemType(item),
    watcherId: getWatcherId(item),
    htmlId: item.id || "",
    id: item.id || "",
    authorName: getText(item, "#author-name"),
    owner: item.getElementsByClassName("owner").length > 0,
    authorPhotoUrl: getImageUrl(item),
    message: getText(item, "#message"),
    timestamp: getText(item, "#timestamp"),
    rawText: normalizeText(item.innerText || item.textContent || ""),
    receivedAt: new Date().toISOString()
  });

  const emitChatItem = (item) => {
    if (state.seenItems.has(item)) {
      return;
    }

    state.seenItems.add(item);
    const chatItem = readChatItem(item);

    if (!chatItem.rawText) {
      return;
    }

    window[bindingName](JSON.stringify(chatItem));
  };

  const queueChatItem = (item) => {
    window.setTimeout(() => emitChatItem(item), 0);
  };

  const scanForChatItems = (node) => {
    if (!(node instanceof Element)) {
      return;
    }

    if (node.matches(chatItemSelector)) {
      queueChatItem(node);
    }

    node.querySelectorAll(chatItemSelector).forEach(queueChatItem);
  };

  if (includeExistingOnStart) {
    document.querySelectorAll(chatItemSelector).forEach(queueChatItem);
  } else {
    document.querySelectorAll(chatItemSelector).forEach((item) => {
      state.seenItems.add(item);
    });
  }

  state.observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      mutation.addedNodes.forEach(scanForChatItems);
    });
  });

  state.observer.observe(document.body, {
    childList: true,
    subtree: true
  });

  return "watcher installed";
})()
"""

WATCHER_CLEANUP_JS = r"""
(() => {
  if (window.__ytChatPythonWatcher?.observer) {
    window.__ytChatPythonWatcher.observer.disconnect();
    delete window.__ytChatPythonWatcher;
    return "watcher removed";
  }

  return "watcher not installed";
})()
"""


class WebSocketConnectionClosed(RuntimeError):
    pass


class WatchUnavailable(RuntimeError):
    pass


class DevToolsWebSocket:
    def __init__(self, url: str) -> None:
        self.url = url
        self.sock: socket.socket | None = None

    def __enter__(self) -> "DevToolsWebSocket":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        self.close()

    def connect(self) -> None:
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme != "ws":
            raise ValueError(f"Only ws:// DevTools URLs are supported: {self.url}")

        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"

        sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = sock.recv(4096)

        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            sock.close()
            raise ConnectionError(f"WebSocket handshake failed: {response!r}")

        sock.settimeout(None)
        self.sock = sock

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_pong(self, payload: bytes) -> None:
        self._send_frame(0xA, payload)

    def recv_text(self) -> str:
        chunks: list[bytes] = []
        message_opcode: int | None = None

        while True:
            fin, opcode, payload = self._recv_frame()

            if opcode == 0x8:
                raise WebSocketConnectionClosed("DevTools WebSocket closed")
            if opcode == 0x9:
                self.send_pong(payload)
                continue
            if opcode == 0xA:
                continue
            if opcode != 0x0:
                message_opcode = opcode

            chunks.append(payload)

            if fin:
                break

        data = b"".join(chunks)
        if message_opcode != 0x1:
            raise ValueError(f"Expected text frame, got opcode {message_opcode}")
        return data.decode("utf-8")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.sock is None:
            raise RuntimeError("WebSocket is not connected")

        first_byte = 0x80 | opcode
        length = len(payload)

        if length < 126:
            header = struct.pack("!BB", first_byte, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", first_byte, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first_byte, 0x80 | 127, length)

        mask = os.urandom(4)
        masked_payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked_payload)

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        if self.sock is None:
            raise RuntimeError("WebSocket is not connected")

        header = self._recv_exact(2)
        first_byte, second_byte = header
        fin = bool(first_byte & 0x80)
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        length = second_byte & 0x7F

        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]

        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)

        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

        return fin, opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        if self.sock is None:
            raise RuntimeError("WebSocket is not connected")

        chunks = []
        remaining = length

        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise WebSocketConnectionClosed("DevTools WebSocket closed")
            chunks.append(chunk)
            remaining -= len(chunk)

        return b"".join(chunks)


class DevToolsClient:
    def __init__(self, websocket: DevToolsWebSocket) -> None:
        self.websocket = websocket
        self.next_message_id = 1
        self.event_handler: Callable[[dict], None] | None = None

    def call(self, method: str, params: dict | None = None) -> dict:
        message_id = self.next_message_id
        self.next_message_id += 1

        payload = {
            "id": message_id,
            "method": method,
            "params": params or {},
        }
        self.websocket.send_text(json.dumps(payload))

        while True:
            message = json.loads(self.websocket.recv_text())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})

            if self.event_handler is not None:
                self.event_handler(message)

    def events(self):  # type: ignore[no-untyped-def]
        while True:
            yield json.loads(self.websocket.recv_text())


class BrowserTab:
    """Small helper for command handlers that need to touch the attached tab."""

    def __init__(self, client: DevToolsClient) -> None:
        self.client = client

    def evaluate(self, expression: str, *, await_promise: bool = True):  # type: ignore[no-untyped-def]
        result = self.client.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            exception = details.get("exception", {})
            raise RuntimeError(exception.get("description") or details.get("text") or "JavaScript failed")

        remote_object = result.get("result", {})
        if "value" in remote_object:
            return remote_object["value"]
        if remote_object.get("type") == "undefined":
            return None
        return remote_object.get("description")

    def click(self, selector: str) -> bool:
        selector_json = json.dumps(selector)
        return bool(
            self.evaluate(
                f"""
(() => {{
  const element = document.querySelector({selector_json});
  if (!element) {{
    return false;
  }}
  element.click();
  return true;
}})()
"""
            )
        )

    def set_text(self, selector: str, text: str) -> bool:
        selector_json = json.dumps(selector)
        text_json = json.dumps(text)
        return bool(
            self.evaluate(
                f"""
(() => {{
  document.querySelectorAll({selector_json}).forEach(element => {{
    if (!element) {{
        return false;
    }}

    const text = {text_json};
    element.focus?.();
    if ("value" in element) {{
        element.value = text;
    }} else {{
        element.textContent = text;
    }}
    element.dispatchEvent(new Event("input", {{ bubbles: true }}));
    element.dispatchEvent(new Event("change", {{ bubbles: true }}));
    return true;
  }});
}})()
"""
            )
        )

    def get_text(self, selector: str) -> str:
        selector_json = json.dumps(selector)
        value = self.evaluate(
            f"""
(() => {{
  const element = document.querySelector({selector_json});
  return element ? (element.innerText || element.textContent || "") : "";
}})()
"""
        )
        return str(value or "")

    def hide_chat_item(self, chat_item: dict) -> bool:
        watcher_id = chat_item.get("watcherId")
        if not watcher_id:
            return False

        watcher_id_json = json.dumps(str(watcher_id))
        return bool(
            self.evaluate(
                f"""
(() => {{
  const watcherId = {watcher_id_json};
  const items = Array.from(document.querySelectorAll("[data-yt-chat-python-watcher-id]"));
  const element = items.find((item) => item.dataset.ytChatPythonWatcherId === watcherId);
  if (!element) {{
    return false;
  }}
  element.style.display = "none";
  return true;
}})()
"""
            )
        )


def fetch_tabs(host: str, port: int) -> list[dict]:
    url = f"http://{host}:{port}/json"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise WatchUnavailable(
            "Chrome DevTools is not reachable. Start Chrome with "
            f"--remote-debugging-port={port} first. Details: {error}"
        ) from error


def choose_tab(tabs: list[dict], url_contains: str) -> dict:
    for tab in tabs:
        if tab.get("type") == "page" and url_contains in tab.get("url", ""):
            return tab

    open_tabs = "\n".join(f"- {tab.get('title', '')}: {tab.get('url', '')}" for tab in tabs)
    raise WatchUnavailable(
        f"No open tab matched {url_contains!r}.\n"
        "Open the YouTube live chat popout tab, then run the script again.\n\n"
        f"Visible DevTools tabs:\n{open_tabs}"
    )


def handle_event(message: dict, browser: BrowserTab | None = None) -> None:
    if message.get("method") != "Runtime.bindingCalled":
        return

    params = message.get("params", {})
    if params.get("name") != BINDING_NAME:
        return

    try:
        chat_item = json.loads(params.get("payload", "{}"))
    except json.JSONDecodeError:
        print(f"Received non-JSON chat payload: {params.get('payload')!r}", file=sys.stderr)
        return

    handle_chat_item(chat_item, browser)


def install_browser_binding(client: DevToolsClient) -> None:
    try:
        client.call("Runtime.removeBinding", {"name": BINDING_NAME})
    except RuntimeError:
        pass

    try:
        client.call("Runtime.addBinding", {"name": BINDING_NAME})
    except RuntimeError as error:
        message = str(error).lower()
        if "already" not in message and "exist" not in message:
            raise


def uninstall_browser_watcher(client: DevToolsClient) -> None:
    try:
        client.call("Runtime.evaluate", {"expression": WATCHER_CLEANUP_JS, "awaitPromise": True})
    except (WebSocketConnectionClosed, ConnectionError, OSError, TimeoutError, RuntimeError):
        pass

    try:
        client.call("Runtime.removeBinding", {"name": BINDING_NAME})
    except (WebSocketConnectionClosed, ConnectionError, OSError, TimeoutError, RuntimeError):
        pass


def print_tabs(tabs: list[dict]) -> None:
    for tab in tabs:
        title = tab.get("title", "")
        url = tab.get("url", "")
        print(f"{tab.get('type', '')}: {title}\n  {url}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Chrome DevTools host")
    parser.add_argument("--port", type=int, default=9222, help="Chrome DevTools port")
    parser.add_argument(
        "--url-contains",
        default="youtube.com/live_chat",
        help="Attach to the first DevTools tab whose URL contains this text",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Also emit chat items already visible when the watcher starts",
    )
    parser.add_argument(
        "--retry-interval",
        type=float,
        default=5.0,
        help="Seconds to wait before reconnecting after Chrome or the chat tab is unavailable",
    )
    parser.add_argument("--list-tabs", action="store_true", help="List DevTools tabs and exit")
    return parser.parse_args()


def watch_once(args: argparse.Namespace) -> None:
    tabs = fetch_tabs(args.host, args.port)
    tab = choose_tab(tabs, args.url_contains)
    websocket_url = tab.get("webSocketDebuggerUrl")
    if not websocket_url:
        raise WatchUnavailable("The selected tab does not expose a DevTools WebSocket URL.")

    print(f"Attached to: {tab.get('title', '')}", flush=True)

    with DevToolsWebSocket(websocket_url) as websocket:
        client = DevToolsClient(websocket)
        browser = BrowserTab(client)
        client.event_handler = lambda event: handle_event(event, browser)
        client.call("Runtime.enable")
        install_browser_binding(client)
        watcher_js = WATCHER_JS.replace(
            "__INCLUDE_EXISTING_ON_START__",
            "true" if args.include_existing else "false",
        )
        result = client.call("Runtime.evaluate", {"expression": watcher_js, "awaitPromise": True})
        install_result = result.get("result", {}).get("value")
        print(f"Browser watcher: {install_result}", flush=True)

        try:
            for event in client.events():
                handle_event(event, browser)
        finally:
            uninstall_browser_watcher(client)


def main() -> int:
    args = parse_args()

    if args.list_tabs:
        try:
            print_tabs(fetch_tabs(args.host, args.port))
        except WatchUnavailable as error:
            print(error, file=sys.stderr, flush=True)
            return 1
        return 0

    print("Waiting for Chrome DevTools and YouTube live chat...", flush=True)
    last_error = ""
    retry_interval = max(args.retry_interval, 0.1)

    try:
        while True:
            try:
                watch_once(args)
                last_error = ""
            except (WatchUnavailable, WebSocketConnectionClosed, ConnectionError, OSError, TimeoutError) as error:
                message = str(error)
                if message != last_error:
                    print(f"Disconnected: {message}", file=sys.stderr, flush=True)
                    print(f"Retrying in {retry_interval:g} seconds...", flush=True)
                    last_error = message
                time.sleep(retry_interval)
    except KeyboardInterrupt:
        print("Stopped.", flush=True)

    return 0


def command_accepts_browser(command: Callable) -> bool:
    try:
        parameters = inspect.signature(command).parameters
    except (TypeError, ValueError):
        return False

    return "browser" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def run_stream_command(command: Callable, chat_item: dict, browser: BrowserTab | None) -> None:
    if command_accepts_browser(command):
        command(chat_item, browser=browser)
    else:
        command(chat_item)


def handle_chat_item(chat_item: dict, browser: BrowserTab | None = None) -> None:
    """Do your bot work here for each new chat item."""
    author = chat_item.get("authorName") or "(unknown)"
    message = chat_item.get("message") or chat_item.get("rawText") or ""
    timestamp = chat_item.get("timestamp") or "--"
    
    if (message.startswith("!")):
        command_name = message.split(maxsplit=1)[0]
        command = stream_commands.STREAM_COMMANDS.get(command_name)
        if command is None:
            run_stream_command(stream_commands.delete_command, chat_item, browser)
            print(f"[{timestamp}] {author}: unknown command {command_name}", flush=True)
            return
        try:
            run_stream_command(command, chat_item, browser)
        except Exception as error:
            print(f"[{timestamp}] {author}: command {command_name} failed: {error}", file=sys.stderr, flush=True)
        finally:
            try:
                stream_commands.delete_command(chat_item, browser=browser)
            except Exception as error:
                print(
                    f"[{timestamp}] {author}: command cleanup failed: {error}",
                    file=sys.stderr,
                    flush=True,
                )
    else:
        print(f"[{timestamp}] {author}: {message}", flush=True)

if __name__ == "__main__":
    raise SystemExit(main())
