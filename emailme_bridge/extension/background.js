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

browser.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "emailme-send") return;

  const payload = {
    url: tab.url,
    title: tab.title,
    selection: info.selectionText || "",
    linkUrl: info.linkUrl || "",
    srcUrl: info.srcUrl || ""
  };

  browser.runtime.sendNativeMessage("com.klif.emailme_bridge", payload)
    .then(response => console.log("Native host responded:", response))
    .catch(error => console.error("Native messaging error:", error));
});

browser.browserAction.onClicked.addListener(async (tab) => {
  const payload = {
    url: tab.url,
    title: tab.title,
    selection: "",
    linkUrl: "",
    srcUrl: ""
  };

  browser.runtime.sendNativeMessage("com.klif.emailme_bridge", payload)
    .then(response => console.log("Native host responded:", response))
    .catch(error => console.error("Native messaging error:", error));
});