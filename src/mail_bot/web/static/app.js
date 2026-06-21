"use strict";

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!resp.ok) {
    let msg = `请求失败 (${resp.status})`;
    try {
      const j = await resp.json();
      if (j && j.error) msg = j.error;
    } catch (_) {}
    throw new Error(msg);
  }
  return resp;
}

function showError(err) {
  alert("操作失败：" + (err && err.message ? err.message : err));
}

// --- Kanban drag & drop -----------------------------------------------------
function initDragDrop() {
  document.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("dragstart", () => card.classList.add("dragging"));
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
  });

  document.querySelectorAll(".dropzone").forEach((zone) => {
    zone.addEventListener("dragover", (e) => {
      e.preventDefault();
      zone.classList.add("drag-over");
      const dragging = document.querySelector(".dragging");
      if (!dragging) return;
      const after = getDragAfter(zone, e.clientY);
      if (after == null) zone.appendChild(dragging);
      else zone.insertBefore(dragging, after);
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
    zone.addEventListener("drop", async (e) => {
      e.preventDefault();
      zone.classList.remove("drag-over");
      const status = zone.dataset.status;
      const ids = [...zone.querySelectorAll(".card")].map((c) => Number(c.dataset.id));
      try {
        await postJSON("/api/board/reorder", { status, ordered_ids: ids });
      } catch (err) {
        showError(err);
        location.reload(); // resync the board to the server's real order
      }
    });
  });
}

function getDragAfter(zone, y) {
  const els = [...zone.querySelectorAll(".card:not(.dragging)")];
  let closest = { offset: -Infinity, element: null };
  for (const child of els) {
    const box = child.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    if (offset < 0 && offset > closest.offset) closest = { offset, element: child };
  }
  return closest.element;
}

// --- Priority quick-set (board) ---------------------------------------------
function initPriority() {
  document.querySelectorAll(".prio-set .pbtn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      const wrap = btn.closest(".prio-set");
      const id = wrap.dataset.id;
      const prio = btn.dataset.prio;
      try {
        await postJSON(`/api/event/${id}/edit`, { priority: prio });
      } catch (err) {
        showError(err);
        return; // do not optimistically update the UI on failure
      }
      wrap.querySelectorAll(".pbtn").forEach((b) => b.classList.toggle("on", b === btn));
      const card = btn.closest(".card");
      if (card) {
        card.classList.remove("prio-P0", "prio-P1", "prio-P2");
        card.classList.add("prio-" + prio);
        const badge = card.querySelector(".badge.prio");
        if (badge) badge.textContent = prio;
      }
    });
  });
}

// --- Buttons that act then reload (only on success) -------------------------
function bind(selector, handler) {
  document.querySelectorAll(selector).forEach((el) => {
    el.addEventListener("click", async () => {
      try {
        await handler(el);
      } catch (err) {
        showError(err);
      }
    });
  });
}

function initActions() {
  bind(".archive-btn", async (btn) => {
    const archived = btn.dataset.archived === "true";
    await postJSON(`/api/event/${btn.dataset.id}/archive`, { archived: !archived });
    location.reload();
  });

  const reBtn = document.getElementById("reaggregate");
  if (reBtn) {
    reBtn.addEventListener("click", async () => {
      reBtn.disabled = true;
      reBtn.textContent = "重新归并中…";
      try {
        await postJSON(`/api/event/${reBtn.dataset.id}/reaggregate`, {});
        location.reload();
      } catch (err) {
        showError(err);
        reBtn.disabled = false;
        reBtn.textContent = "用 LLM 重新归并本事件";
      }
    });
  }

  const mergeBtn = document.getElementById("merge");
  if (mergeBtn) {
    mergeBtn.addEventListener("click", async () => {
      const ids = [...document.querySelectorAll(".merge-src:checked")].map((c) => Number(c.value));
      if (!ids.length) return;
      try {
        await postJSON(`/api/event/${mergeBtn.dataset.id}/merge`, { source_ids: ids });
        location.href = `/event/${mergeBtn.dataset.id}`;
      } catch (err) {
        showError(err);
      }
    });
  }

  bind(".split-btn", async (btn) => {
    await postJSON(`/api/email/${btn.dataset.id}/split`, {});
    location.reload();
  });

  bind(".move-btn", async (btn) => {
    const item = btn.closest(".email-item");
    const sel = item.querySelector(".move-target");
    const target = sel && sel.value;
    if (!target) return;
    await postJSON(`/api/email/${btn.dataset.id}/move`, { target_event_id: Number(target) });
    location.reload();
  });
}

initDragDrop();
initPriority();
initActions();
