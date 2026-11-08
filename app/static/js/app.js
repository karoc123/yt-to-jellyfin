function toggleAdvancedOptions() {
  const adv = document.getElementById("advanced-options");
  if (adv) {
    adv.classList.toggle("open");
  }
}

// Multi-select bulk management
let selectedVideoIds = new Set();

function updateBulkBar() {
  const bar = document.getElementById("bulk-bar");
  const countEl = document.getElementById("bulk-count");
  if (!bar || !countEl) return;

  const count = selectedVideoIds.size;
  if (count > 0) {
    countEl.textContent = `${count} Video${count > 1 ? "s" : ""} ausgewählt`;
  }
  bar.hidden = count === 0;
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
  if (!confirm(`Möchtest du wirklich ${selectedVideoIds.size} Video(s) unwiderruflich löschen?`)) {
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
      alert("Fehler beim Löschen der Videos.");
    }
  } catch (err) {
    alert("Netzwerkfehler: " + err);
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
      alert("Fehler beim Anpassen des Pin-Status.");
    }
  } catch (err) {
    alert("Netzwerkfehler: " + err);
  }
}
