@echo off
set "CHAT_URL=https://www.youtube.com/live_chat?is_popout=1&v=VIDEO_CODE"

"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%TEMP%\yt-chat-obs-debug" --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling "%CHAT_URL%"
