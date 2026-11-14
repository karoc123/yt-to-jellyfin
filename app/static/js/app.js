// Multi-select bulk management (library channel view)
let selectedVideoIds = new Set();

function updateBulkBar() {
  const bar = document.getElementById("bulk-bar");
  const countEl = document.getElementById("bulk-count");
  if (!bar || !countEl) return;

  const count = selectedVideoIds.size;
  countEl.textContent = `${count} selected`;
  bar.hidden = count === 0;
  document.getElementById("vids")?.classList.toggle("selecting", count > 0);
  document.querySelectorAll(".video-checkbox").forEach((cb) => {
    cb.closest(".vid")?.classList.toggle("sel", cb.checked);
  });
}

function toggleSelectVideo(checkbox) {
  const vidId = checkbox.dataset.videoId;
  if (checkbox.checked) {
    selectedVideoIds.add(vidId);
  } else {
    selectedVideoIds.delete(vidId);
  }
  updateBulkBar();
}

function selectAllVideos(onlyUnpinned = false) {
  document.querySelectorAll(".video-checkbox").forEach((cb) => {
    if (onlyUnpinned && cb.dataset.pinned === "1") {
      cb.checked = false;
      selectedVideoIds.delete(cb.dataset.videoId);
    } else {
      cb.checked = true;
      selectedVideoIds.add(cb.dataset.videoId);
    }
  });
  updateBulkBar();
}

function deselectAllVideos() {
  document.querySelectorAll(".video-checkbox").forEach((cb) => {
    cb.checked = false;
  });
  selectedVideoIds.clear();
  updateBulkBar();
}

async function bulkDeleteVideos() {
  if (selectedVideoIds.size === 0) return;
  if (!confirm(`Delete ${selectedVideoIds.size} video(s) from the library and disk?`)) {
    return;
  }

  const ids = Array.from(selectedVideoIds);
  try {
    const res = await fetch("/api/videos/bulk-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_ids: ids }),
    });
    if (res.ok) {
      window.location.reload();
    } else {
      alert("Failed to delete videos.");
    }
  } catch (err) {
    alert("Network error: " + err);
  }
}

async function bulkPinVideos(pinState) {
  if (selectedVideoIds.size === 0) return;
  const ids = Array.from(selectedVideoIds);
  try {
    const res = await fetch("/api/videos/bulk-pin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_ids: ids, is_pinned: pinState }),
    });
    if (res.ok) {
      window.location.reload();
    } else {
      alert("Failed to update pin status.");
    }
  } catch (err) {
    alert("Network error: " + err);
  }
}

// Subscriptions: inline edit form below a row
function toggleSubEdit(el) {
  const sub = el.closest(".sub");
  const form = sub?.querySelector(".sub-edit");
  const trigger = sub?.querySelector("[aria-controls]");
  if (!form) return;
  form.hidden = !form.hidden;
  trigger?.setAttribute("aria-expanded", String(!form.hidden));
  if (!form.hidden) form.querySelector("select, input:not([type=hidden])")?.focus();
}

// Subscriptions: remove dialog switches its submit button between unsubscribe and delete
document.addEventListener("change", (event) => {
  const radio = event.target;
  if (!(radio instanceof HTMLInputElement) || radio.name !== "mode") return;
  const form = radio.closest(".remove-form");
  const button = form?.querySelector("button[type=submit]");
  if (!button) return;
  const destructive = radio.value === "delete_all";
  button.textContent = button.dataset[destructive ? "labelDelete_all" : "labelUnsubscribe"];
  button.classList.toggle("btn-danger-solid", destructive);
});

// Settings: highlight the section currently in view
document.addEventListener("DOMContentLoaded", () => {
  const links = document.querySelectorAll(".snav a");
  if (!links.length || !("IntersectionObserver" in window)) return;
  const setCurrent = (id) => {
    links.forEach((a) => {
      if (a.getAttribute("href") === `#${id}`) a.setAttribute("aria-current", "true");
      else a.removeAttribute("aria-current");
    });
  };
  setCurrent(links[0].getAttribute("href").slice(1));
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) setCurrent(entry.target.id);
      });
    },
    { rootMargin: "-80px 0px -65% 0px" },
  );
  document.querySelectorAll(".sec[id]").forEach((sec) => observer.observe(sec));
});
