"""Command handlers used by youtube_chat_watcher.py."""

import youtube_api

def require_owner(data):
    return data.get("owner", False)


def test_command(data, browser=None):
    if not require_owner(data):
        return
    print(data)


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


def youtube_api_demo_command(data, browser=None):
    """Post a message through the YouTube API and try a native delete."""
    if not require_owner(data):
        return
    if browser is None:
        print("Cannot run YouTube API demo because no browser tab is attached")
        return

    video_id = get_current_video_id(browser)
    if not video_id:
        print("Could not find a YouTube video ID from the chat tab URL")
        return

    text = command_text_after(data, "!ytapi")
    if not text:
        text = "Native YouTube API demo from the OBS chat bot."

    api = youtube_api.YouTubeApi()
    sent_message = api.send_live_chat_message_for_video(video_id, text)
    sent_id = sent_message.get("id") or "(no id returned)"
    print(f"Sent native YouTube chat message: {sent_id}")

    command_message_id = youtube_api.extract_live_chat_message_id(data)
    if not command_message_id:
        if browser.hide_chat_item(data):
            print("No API message ID was found, so the command was hidden locally")
        return

    try:
        api.delete_live_chat_message(command_message_id)
        print(f"Deleted command message natively: {command_message_id}")
    except youtube_api.YouTubeApiError as error:
        print(f"Native delete failed for {command_message_id}: {error}")
        if browser.hide_chat_item(data):
            print("Command message was hidden locally instead")


def youtube_hide_command(data, browser=None):
    """Delete a live chat message by API ID.

    Usage in chat: !ythide MESSAGE_ID
    If no ID is supplied, this tries to delete the command message itself.
    """
    if not require_owner(data):
        return

    message_id = command_text_after(data, "!ythide")
    if not message_id:
        message_id = youtube_api.extract_live_chat_message_id(data)
    if not message_id:
        print("Usage: !ythide LIVE_CHAT_MESSAGE_ID")
        return

    api = youtube_api.YouTubeApi()
    api.delete_live_chat_message(message_id)
    print(f"Deleted live chat message natively: {message_id}")


STREAM_COMMANDS = {
    "!test": test_command,
    "!clear": clear_command,
    "!ytapi": youtube_api_demo_command,
    "!ythide": youtube_hide_command,
}
