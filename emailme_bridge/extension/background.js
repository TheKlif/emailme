browser.contextMenus.create({
  id: "emailme-send",
  title: "#emailme",
  contexts: ["selection", "image", "link", "page"]
});

browser.contextMenus.onShown.addListener((info) => {
  let label = "#emailme";
  if (info.selectionText) {
    label = "#emailme this text";
  } else if (info.linkUrl) {
    label = "#emailme this link";
  } else if (info.srcUrl) {
    label = "#emailme this image";
  } else {
    label = "#emailme this page";
  }
  browser.contextMenus.update("emailme-send", { title: label });
  browser.contextMenus.refresh();
});

// --- METHOD 1: Windows native toast ---
function notifyWindowsToast(success) {
  browser.notifications.create({
    type: "basic",
    iconUrl: browser.runtime.getURL("icon-48.png"),
    title: success ? "Saved" : "Save failed",
    message: success ? "Sent to emailme" : "emailme couldn't save that"
  });
}

// --- METHOD 2: toolbar badge flash ---
function notifyBadgeFlash(success) {
  browser.browserAction.setBadgeText({ text: success ? "✓" : "✗" });
  browser.browserAction.setBadgeBackgroundColor({ color: success ? "#2e7d32" : "#c62828" });
  setTimeout(() => browser.browserAction.setBadgeText({ text: "" }), 2000);
}

// METHOD 3: in-page floating toast, top-right with icon
function notifyPageToast(success, tabId) {
  const message = success ? "✓ Saved to emailme" : "✗ emailme save failed";
  const bgColor = success ? "#171717" : "#b91c1c";
  
  // Resolve the extension-local image path to a browser-accessible URL
  const iconUrl = browser.runtime.getURL("icon-48.png");

  browser.tabs.executeScript(tabId, {
    code: `
      (function() {
        const toast = document.createElement("div");
        // Added display:flex, align-items:center, and gap:12px to handle layout
        toast.style.cssText = "position:fixed;top:20px;right:20px;background:${bgColor};color:white;padding:16px 24px;border-radius:8px;font-family:sans-serif;font-size:18px;font-weight:600;z-index:2147483647;box-shadow:0 4px 16px rgba(0,0,0,0.4);opacity:0;transform:translateX(40px);transition:opacity 0.25s ease, transform 0.25s ease;display:flex;align-items:center;gap:12px;";
        
        const img = document.createElement("img");
        img.src = ${JSON.stringify(iconUrl)};
        img.style.cssText = "width:48px;height:48px;display:block;";
        
        const textNode = document.createElement("span");
        textNode.textContent = ${JSON.stringify(message)};
        
        toast.appendChild(img);
        toast.appendChild(textNode);
        
        document.body.appendChild(toast);
        
        requestAnimationFrame(() => {
          toast.style.opacity = "1";
          toast.style.transform = "translateX(0)";
        });
        
        setTimeout(() => {
          toast.style.opacity = "0";
          toast.style.transform = "translateX(40px)";
          setTimeout(() => toast.remove(), 250);
        }, 3500);
      })();
    `
  }).catch(err => console.warn("Page toast injection failed (restricted page?):", err));
}

function notifyAll(success, tabId) {
  // notifyWindowsToast(success);
  notifyBadgeFlash(success);     // comment out to disable
  notifyPageToast(success, tabId); // comment out to disable
}

function sendAndNotify(payload, tabId) {
  browser.runtime.sendNativeMessage("com.klif.emailme_bridge", payload)
    .then(response => {
      console.log("Native host responded:", response);
      notifyAll(response && response.status === "ok", tabId);
    })
    .catch(error => {
      console.error("Native messaging error:", error);
      notifyAll(false, tabId);
    });
}

browser.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "emailme-send") return;
  const payload = {
    url: tab.url,
    title: tab.title,
    selection: info.selectionText || "",
    linkUrl: info.linkUrl || "",
    srcUrl: info.srcUrl || ""
  };
  sendAndNotify(payload, tab.id);
});

browser.browserAction.onClicked.addListener(async (tab) => {
  const payload = {
    url: tab.url,
    title: tab.title,
    selection: "",
    linkUrl: "",
    srcUrl: ""
  };
  sendAndNotify(payload, tab.id);
});