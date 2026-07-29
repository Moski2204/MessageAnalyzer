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
  const number = new Intl.NumberFormat();
  let stopped = false;

  const poll = async () => {
    if (stopped) return;
    try {
      const response = await fetch(statusUrl, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("status unavailable");
      const job = await response.json();

      status.textContent = job.status.charAt(0).toUpperCase() + job.status.slice(1);
      stage.textContent = job.stage;
      count.textContent = `${number.format(job.processed_messages)} of ${number.format(job.total_messages)}`;
      elapsed.textContent = `${Number(job.elapsed_seconds).toFixed(1)} seconds`;

      if (job.percentage === null) {
        progress.removeAttribute("value");
        percentage.textContent = "Working";
      } else {
        const value = Math.max(0, Math.min(100, Number(job.percentage)));
        progress.value = value;
        percentage.textContent = `${value.toFixed(1)}%`;
      }

      if (job.status === "complete" && job.result_url) {
        stopped = true;
        window.location.replace(job.result_url);
        return;
      }
      if (job.status === "failed" || job.status === "interrupted") {
        stopped = true;
        error.textContent = job.error || "Analysis stopped before completion.";
        error.classList.remove("hidden");
        return;
      }
    } catch (_error) {
      stage.textContent = "Waiting for the local application";
    }
    window.setTimeout(poll, 1500);
  };

  window.setTimeout(poll, 500);
})();
