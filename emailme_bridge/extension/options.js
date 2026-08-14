const input = document.getElementById("targetFolder");
const status = document.getElementById("status");

browser.storage.local.get("targetFolder").then((result) => {
  if (result.targetFolder) {
    input.value = result.targetFolder;
  }
});

document.getElementById("save").addEventListener("click", () => {
  const value = input.value.trim();
  browser.storage.local.set({ targetFolder: value }).then(() => {
    status.textContent = "Saved.";
    setTimeout(() => {
      status.textContent = "";
    }, 2000);
  });
});
