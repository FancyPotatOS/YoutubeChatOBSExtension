
# Youtube Chat OBS Extension

This is a Google Chrome extension that formats a Youtube Chat tab to look better in OBS!

The design is extremely simple, and allows for the .css to be modified to enable more customization.

## Setup

1. Download the repository
2. Extract into a folder
3. Go to [Chrome Extensions](chrome://extensions) on your browser
4. Select 'Load unpacked' at the top
5. Select the folder containing the files

## How to use

1. Open your Youtube stream chat on Chrome (ex. www.youtube.com/live_chat?is_popout=1&v=<VIDEO_CODE>)

4. Click on the extension to modify the tab:

<img width="255" height="120" alt="image" src="https://github.com/user-attachments/assets/472eab6c-8442-48dd-8c9e-b946e970f555" />

There is now a simple design in the bottom left to use in OBS.

<img width="895" height="1189" alt="image" src="https://github.com/user-attachments/assets/69fb87ff-ba1e-46e4-a6a2-47dfc550a0f7" />

## Notes

All this extension does is apply some CSS to clean up the design, so it's nothing special. The sections in the greenscreen chat is scaled up, so you'll have to check it manually to crop it appropriately.

The tab with Youtube chat cannot be minimized, otherwise it will resize to 0x0 in OBS.

## Watching new chat items

Clicking the extension now also starts a `MutationObserver` in the chat tab. Each new chat item is parsed into:

```js
{
  type,
  id,
  authorName,
  authorPhotoUrl,
  message,
  timestamp,
  rawText,
  receivedAt
}
```

Edit `performAction` in `scripts.js` to run browser-side bot behavior for each new item. The current default logs each item to the live chat tab console and dispatches a `yt-chat-obs:new-item` browser event.

Existing visible messages are ignored when the watcher starts. Set `PROCESS_EXISTING_ON_START` to `true` in `scripts.js` if you want to process the currently visible chat backlog too.

## Optional Python watcher

`youtube_chat_watcher.py` can read new chat items from an open Chrome tab and print them in a terminal. Start Chrome with a DevTools port first:

```powershell
"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="C:\YoutubeChatOBSExtension" --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --remote-debugging-port=9222 --user-data-dir="$env:TEMP\yt-chat-obs-debug" "https://www.youtube.com/live_chat?is_popout=1&v=VIDEO_CODE"
```

Then run:

```powershell
python .\youtube_chat_watcher.py
```

Add `--include-existing` if you also want to print the messages already visible when the watcher attaches.

Put Python-side bot behavior in `handle_chat_item` inside `youtube_chat_watcher.py`.

The Python watcher can be started before Chrome is open. It waits for the DevTools port and live chat tab, and reconnects if Chrome or the tab is closed.
Use `--retry-interval 2` to change the reconnect delay.
