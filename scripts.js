(() => {
  const WATCHER_KEY = "__ytChatObsWatcher";
  const CHAT_ITEM_SELECTORS = [
    "yt-live-chat-text-message-renderer",
    "yt-live-chat-paid-message-renderer",
    "yt-live-chat-paid-sticker-renderer",
    "yt-live-chat-membership-item-renderer"
  ];
  const CHAT_ITEM_SELECTOR = CHAT_ITEM_SELECTORS.join(",");
  const PROCESS_EXISTING_ON_START = true;

  console.log("YT Chat OBS Mode enabled");
  document.documentElement.dataset.obsMode = "true";

  if (globalThis[WATCHER_KEY]) {
    console.log("[YT Chat OBS] Chat watcher already running");
    return;
  }

  const state = {
    observer: null,
    seenItems: new WeakSet()
  };
  globalThis[WATCHER_KEY] = state;

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

  const readChatItem = (item) => ({
    type: getChatItemType(item),
    id: item.id || "",
    authorName: getText(item, "#author-name"),
    authorPhotoUrl: getImageUrl(item),
    message: getText(item, "#message"),
    timestamp: getText(item, "#timestamp"),
    rawText: normalizeText(item.innerText || item.textContent || ""),
    receivedAt: new Date().toISOString()
  });

  const performAction = (chatItem, element) => {
    // Put bot actions here. This currently exposes the item in two easy places:
    // the tab console and a browser event other scripts can subscribe to.
    // console.log("[YT Chat OBS] New chat item", chatItem);
    window.dispatchEvent(new CustomEvent("yt-chat-obs:new-item", {
      detail: chatItem
    }));

    if (chatItem.message.startsWith("!")) {
      element.style.display = "none";
    }
  };

  const processChatItem = (item) => {
    if (state.seenItems.has(item)) {
      return;
    }

    state.seenItems.add(item);
    const chatItem = readChatItem(item);

    if (!chatItem.rawText) {
      return;
    }

    performAction(chatItem, item);
  };

  const queueChatItem = (item) => {
    window.setTimeout(() => processChatItem(item), 0);
  };

  const scanForChatItems = (node) => {
    if (!(node instanceof Element)) {
      return;
    }

    if (node.matches(CHAT_ITEM_SELECTOR)) {
      queueChatItem(node);
    }

    node.querySelectorAll(CHAT_ITEM_SELECTOR).forEach(queueChatItem);
  };

  if (PROCESS_EXISTING_ON_START) {
    document.querySelectorAll(CHAT_ITEM_SELECTOR).forEach(queueChatItem);
  } else {
    document.querySelectorAll(CHAT_ITEM_SELECTOR).forEach((item) => {
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

  console.log("[YT Chat OBS] Chat watcher enabled");
})();
