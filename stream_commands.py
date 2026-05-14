"""Command handlers used by youtube_chat_watcher.py."""

import stream_inventory
import youtube_api


_LIVE_CHAT_ID_BY_VIDEO_ID = {}


def require_owner(data):
    return data.get("owner", False)


def delete_command(data, browser=None):
    """Delete the command message itself by matching it through the API."""
    if browser is None:
        return False

    message = data.get("message") or data.get("rawText") or ""
    if not message.startswith("!"):
        return False

    api = youtube_api.YouTubeApi()
    live_chat_id = get_current_live_chat_id(api, browser)
    if not live_chat_id:
        return False

    try:
        deleted = api.delete_recent_message(
            live_chat_id,
            text=message,
            author=data.get("authorName"),
            received_at=data.get("receivedAt"),
        )
    except (youtube_api.YouTubeAuthError, youtube_api.YouTubeApiError) as error:
        print(f"Could not delete command natively: {error}")
        if browser.hide_chat_item(data):
            print("Command message was hidden locally instead")
        return False

    if deleted:
        print(
            "Deleted command message natively: "
            f"{deleted.get('id')} from {youtube_api.live_chat_item_author(deleted)}"
        )
        return True

    if browser.hide_chat_item(data):
        print("Could not match the command in the API list, so it was hidden locally")
    return False


def clear_command(data, browser=None):
    if not require_owner(data):
        return
    
    if browser is None:
        print("Cannot clear chat item because no browser tab is attached")
        return
    
    if browser.set_text("yt-live-chat-text-message-renderer", ""):
        print(f"Cleared command from {data.get('authorName') or '(unknown)'}")
    else:
        print("Could not find the command message in the browser tab")


def command_text_after(data, command_name):
    message = data.get("message") or data.get("rawText") or ""
    if not message.startswith(command_name):
        return ""
    return message[len(command_name):].strip()


def get_current_video_id(browser):
    if browser is None:
        return None

    current_url = browser.evaluate("window.location.href")
    return youtube_api.extract_video_id_from_url(str(current_url or ""))


def get_current_live_chat_id(api, browser):
    video_id = get_current_video_id(browser)
    if not video_id:
        return None

    if video_id not in _LIVE_CHAT_ID_BY_VIDEO_ID:
        _LIVE_CHAT_ID_BY_VIDEO_ID[video_id] = api.get_live_chat_id_for_video(video_id)
    return _LIVE_CHAT_ID_BY_VIDEO_ID[video_id]


def print_purge_result(result):
    deleted = len(result.get("deleted") or [])
    failed = len(result.get("failed") or [])
    scanned = result.get("scanned", 0)
    matched = result.get("matched", 0)
    prefix = result.get("prefix", "!")
    print("Deleted API message IDs:")
    for item in result.get("deleted") or []:
        print(
            f"- {item.get('id')} from {item.get('author') or '(unknown)'}: "
            f"{item.get('text')}"
        )
    print(
        f"Scanned {scanned} API messages; matched {matched} starting with "
        f"{prefix!r}; deleted {deleted}; failed {failed}"
    )
    for item in result.get("failed") or []:
        print(
            f"Could not delete {item.get('id')} from "
            f"{item.get('author') or '(unknown)'}: {item.get('error')}"
        )


def youtube_purge_commands_command(data, browser=None):
    """Delete recent live chat messages whose API text starts with a prefix."""
    if not require_owner(data):
        return
    if browser is None:
        print("Cannot purge YouTube messages because no browser tab is attached")
        return

    prefix = command_text_after(data, "!ytpurge") or "!"
    api = youtube_api.YouTubeApi()
    live_chat_id = get_current_live_chat_id(api, browser)
    if not live_chat_id:
        print("Could not find a YouTube live chat ID from the chat tab URL")
        return

    result = api.delete_recent_messages_by_prefix(live_chat_id, prefix=prefix)
    print_purge_result(result)


STREAM_COMMANDS = {
    "!auction": stream_inventory.auction_item,
    "!clear": clear_command,
    "!register": stream_inventory.register_user,
    "!sell": stream_inventory.sell_item,
    "!ytpurge": youtube_purge_commands_command,
}
