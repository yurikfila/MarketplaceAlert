(function () {
  "use strict";

  const FLASH_KEY = "dashboardFlashMessage";

  function showBanner(text, level) {
    const banner = document.getElementById("banner");
    if (!banner) return;
    banner.textContent = text;
    banner.className = "banner banner-" + level;
    banner.hidden = false;
  }

  function showFlashFromStorage() {
    const raw = sessionStorage.getItem(FLASH_KEY);
    if (!raw) return;
    sessionStorage.removeItem(FLASH_KEY);
    try {
      const parsed = JSON.parse(raw);
      showBanner(parsed.text, parsed.level);
    } catch (err) {
      // Malformed stored message - nothing useful to show.
    }
  }

  function reloadWithMessage(text, level) {
    sessionStorage.setItem(FLASH_KEY, JSON.stringify({ text: text, level: level }));
    window.location.reload();
  }

  async function extractErrorMessage(response) {
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") {
        return body.detail;
      }
    } catch (err) {
      // Response wasn't JSON - fall through to the generic message below.
    }
    return "Request failed (" + response.status + ")";
  }

  function getCheckedMarketplaces(form) {
    return Array.from(form.querySelectorAll('input[name="marketplaces"]:checked')).map(
      function (checkbox) {
        return checkbox.value;
      }
    );
  }

  async function createSearch(event) {
    event.preventDefault();
    const form = event.target;
    const marketplaces = getCheckedMarketplaces(form);
    if (marketplaces.length === 0) {
      showBanner("Select at least one marketplace.", "error");
      return;
    }

    const payload = {
      query: form.query.value,
      marketplaces: marketplaces,
      scan_interval_seconds: parseInt(form.scan_interval_seconds.value, 10),
      is_active: form.is_active.checked,
    };

    try {
      const response = await fetch("/saved-searches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        showBanner(await extractErrorMessage(response), "error");
        return;
      }
      reloadWithMessage("Search created.", "success");
    } catch (err) {
      showBanner("Could not reach the server. Please try again.", "error");
    }
  }

  async function runNow(id) {
    try {
      const response = await fetch("/saved-searches/" + id + "/run", { method: "POST" });
      if (!response.ok) {
        showBanner(await extractErrorMessage(response), "error");
        return;
      }
      const result = await response.json();
      reloadWithMessage(
        "Scan completed: " + result.new_count + " new, " + result.already_seen_count + " already seen.",
        "success"
      );
    } catch (err) {
      showBanner("Could not reach the server. Please try again.", "error");
    }
  }

  async function toggleActive(id, currentlyActive) {
    try {
      const response = await fetch("/saved-searches/" + id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !currentlyActive }),
      });
      if (!response.ok) {
        showBanner(await extractErrorMessage(response), "error");
        return;
      }
      reloadWithMessage(currentlyActive ? "Search disabled." : "Search enabled.", "success");
    } catch (err) {
      showBanner("Could not reach the server. Please try again.", "error");
    }
  }

  async function deleteSearch(id) {
    if (!window.confirm("Delete this saved search? This cannot be undone.")) {
      return;
    }
    try {
      const response = await fetch("/saved-searches/" + id, { method: "DELETE" });
      if (!response.ok && response.status !== 204) {
        showBanner(await extractErrorMessage(response), "error");
        return;
      }
      reloadWithMessage("Search deleted.", "success");
    } catch (err) {
      showBanner("Could not reach the server. Please try again.", "error");
    }
  }

  function handleTableClick(event) {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = button.getAttribute("data-id");
    const action = button.getAttribute("data-action");

    if (action === "run") {
      runNow(id);
    } else if (action === "toggle") {
      toggleActive(id, button.getAttribute("data-active") === "true");
    } else if (action === "delete") {
      deleteSearch(id);
    }
  }

  function setupSelectAllMarketplaces() {
    const selectAll = document.getElementById("marketplaces-select-all");
    const checkboxes = document.querySelectorAll('input[name="marketplaces"]');
    if (!selectAll || checkboxes.length === 0) return;

    selectAll.addEventListener("change", function () {
      checkboxes.forEach(function (checkbox) {
        checkbox.checked = selectAll.checked;
      });
    });

    checkboxes.forEach(function (checkbox) {
      checkbox.addEventListener("change", function () {
        selectAll.checked = Array.from(checkboxes).every(function (c) {
          return c.checked;
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    showFlashFromStorage();
    setupSelectAllMarketplaces();

    const form = document.getElementById("create-search-form");
    if (form) {
      form.addEventListener("submit", createSearch);
    }

    const savedSearchesSection = document.getElementById("saved-searches");
    if (savedSearchesSection) {
      savedSearchesSection.addEventListener("click", handleTableClick);
    }
  });
})();
