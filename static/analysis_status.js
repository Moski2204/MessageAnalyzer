(() => {
  "use strict";

  const panel = document.getElementById("analysis-progress");
  if (!panel) return;

  const statusUrl = panel.dataset.statusUrl;
  const status = document.getElementById("analysis-status");
  const percentage = document.getElementById("analysis-percentage");
  const progress = document.getElementById("analysis-progress-bar");
  const stage = document.getElementById("analysis-stage");
  const count = document.getElementById("analysis-count");
  const elapsed = document.getElementById("analysis-elapsed");
  const error = document.getElementById("analysis-error");
  const completion = document.getElementById("analysis-complete");
  const resultLink = document.getElementById("analysis-result-link");
  const number = new Intl.NumberFormat();
  let stopped = panel.dataset.jobStatus === "failed"
    || panel.dataset.jobStatus === "interrupted";

  const localResultUrl = (value) => {
    if (typeof value !== "string" || !value) return null;
    try {
      const candidate = new URL(value, window.location.href);
      return candidate.origin === window.location.origin ? candidate : null;
    } catch (_error) {
      return null;
    }
  };

  const showError = (message) => {
    stopped = true;
    error.textContent = message || "Analysis stopped before completion.";
    error.classList.remove("hidden");
  };

  const updateText = (element, value) => {
    const nextValue = String(value ?? "");
    if (element.textContent !== nextValue) element.textContent = nextValue;
  };

  const poll = async () => {
    if (stopped) return;
    try {
      const response = await fetch(statusUrl, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (response.status === 400 || response.status === 404) {
        showError(
          "This analysis job is no longer available. Return to Analysis to start or open another analysis."
        );
        return;
      }
      if (!response.ok) throw new Error("status unavailable");
      const job = await response.json();
      const jobStatus = typeof job.status === "string" ? job.status : "unknown";

      panel.dataset.jobStatus = jobStatus;
      updateText(status, jobStatus === "queued"
        ? "Waiting"
        : jobStatus.charAt(0).toUpperCase() + jobStatus.slice(1));
      updateText(stage, job.stage);
      updateText(count, `${number.format(job.processed_messages)} of ${number.format(job.total_messages)}`);
      updateText(elapsed, `${Number(job.elapsed_seconds).toFixed(1)} seconds`);

      if (job.percentage === null) {
        progress.removeAttribute("value");
        updateText(percentage, "Working");
      } else {
        const value = Math.max(0, Math.min(100, Number(job.percentage)));
        progress.value = value;
        updateText(percentage, `${value.toFixed(1)}%`);
      }

      if (jobStatus === "complete") {
        const destination = localResultUrl(job.result_url);
        stopped = true;
        if (!destination) {
          showError("Analysis completed, but the local result link was unavailable.");
          return;
        }
        resultLink.href = destination.href;
        completion.classList.remove("hidden");
        completion.setAttribute("aria-hidden", "false");
        window.setTimeout(() => {
          window.location.replace(destination.href);
        }, 800);
        return;
      }
      if (jobStatus === "failed" || jobStatus === "interrupted") {
        showError(job.error);
        return;
      }
    } catch (_error) {
      updateText(stage, "Waiting for the local application");
    }
    window.setTimeout(poll, 1500);
  };

  if (!stopped) window.setTimeout(poll, 500);
})();
