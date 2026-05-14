"""Command handlers used by youtube_chat_watcher.py."""



def test_command(data, browser=None):
    browser.evaluate(f"""console.log("{data.get('authorName') or '(unknown)'} sent the !test command")""")


def clear_command(data, browser=None):
    if browser is None:
        print("Cannot clear chat item because no browser tab is attached")
        return

    if browser.set_text("yt-live-chat-text-message-renderer", ""):
        print(f"Cleared command from {data.get('authorName') or '(unknown)'}")
    else:
        print("Could not find the command message in the browser tab")


STREAM_COMMANDS = {
    "!test": test_command,
    "!clear": clear_command,
}
