(() => {
  "use strict";

  let knownVersion = null;
  let serverWasUnavailable = false;
  let timer = null;

  async function checkForChanges() {
    try {
      const response = await fetch(`/__dev/version?t=${Date.now()}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error("Hot-reload check failed");
      }

      const payload = await response.json();
      if (knownVersion === null) {
        knownVersion = payload.version;
      } else if (serverWasUnavailable || payload.version !== knownVersion) {
        window.location.reload();
        return;
      }
      serverWasUnavailable = false;
    } catch (_error) {
      // A short failure is expected while Werkzeug restarts Flask.
      serverWasUnavailable = true;
    } finally {
      timer = window.setTimeout(checkForChanges, 750);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      window.clearTimeout(timer);
      checkForChanges();
    }
  });

  checkForChanges();
})();
