chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !tab.url?.startsWith("https://www.youtube.com/live_chat")) {
    console.log("This extension only works on YouTube live chat pages: " + tab.url);
    return;
  }

  await chrome.scripting.insertCSS({
    target: { tabId: tab.id },
    files: ["styles.css"]
  });

  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["scripts.js"]
  });
});