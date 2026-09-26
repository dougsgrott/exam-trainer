/* The exam runner's client half. Vanilla, and deliberately small.
 *
 * What it does NOT do is the point. It never replaces the answer controls, and
 * nothing the server sends can: every save is a 204 with an `HX-Trigger` header
 * and no body, so the checkboxes the person is clicking are the same DOM nodes
 * from the moment the page loads until they navigate. That is integration fix 3 --
 * the bug where the second click of a 2-of-4 lands on a detached node and the
 * answer submits with one selection, marked wrong, with nothing in any log.
 *
 * So this file only touches things that hold no input: the answered counter, the
 * question map, the save indicator and the clock.
 */
(function () {
  "use strict";

  var runner = document.getElementById("runner");
  if (!runner) return;

  var saveState = document.getElementById("save-state");
  var answered = document.getElementById("answered");
  var unanswered = document.getElementById("unanswered");
  var clock = document.getElementById("clock");
  var timeField = document.getElementById("time-ms");
  var flag = document.getElementById("flag");
  var shownAt = Date.now();

  /* ------------------------------------------------------------- the clock
   *
   * `remaining` comes from the server on every render and on every save. The
   * countdown below is decoration: it never decides anything, and it is
   * overwritten by the server's number the next time anything is saved. Moving
   * the client clock forward makes the display wrong and buys no time.
   */
  var remaining = parseInt(runner.dataset.remaining, 10);
  if (isNaN(remaining)) remaining = null;

  function format(total) {
    if (total === null) return "";
    if (total < 0) total = 0;
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }

  if (remaining !== null) {
    setInterval(function () {
      remaining = Math.max(0, remaining - 1);
      clock.textContent = format(remaining);
      clock.classList.toggle("low", remaining < 300);
      clock.classList.toggle("out", remaining === 0);
    }, 1000);
  }

  /* --------------------------------------------------- optimistic, then confirmed */

  function setState(state, text) {
    if (!saveState) return;
    saveState.dataset.state = state;
    saveState.textContent = text;
  }

  document.body.addEventListener("htmx:beforeRequest", function () {
    /* The selection has already changed in the DOM -- the browser did it. This is
       only the admission that the server has not agreed yet. */
    setState("saving", "saving…");
  });

  document.body.addEventListener("htmx:responseError", function () {
    setState("failed", "not saved — check your connection");
  });

  document.body.addEventListener("htmx:sendError", function () {
    setState("failed", "not saved — check your connection");
  });

  document.body.addEventListener("examkb:saved", function (event) {
    var detail = event.detail || {};
    setState("saved", "saved");

    if (typeof detail.answered === "number" && answered) {
      answered.textContent = detail.answered;
      if (unanswered) unanswered.textContent = detail.count - detail.answered;
    }
    if (typeof detail.remaining === "number") {
      remaining = detail.remaining;          /* the server's clock wins */
      if (clock) clock.textContent = format(remaining);
    }
    if (typeof detail.position === "number") {
      var cell = runner.querySelector('.qmap a[data-position="' + detail.position + '"]');
      if (cell) {
        if (detail.selected && detail.selected.length) cell.classList.add("answered");
        cell.classList.toggle("flagged", !!detail.flagged);
      }
    }
    if (flag && detail.position === parseInt(runner.dataset.position, 10)) {
      flag.classList.toggle("on", !!detail.flagged);
      flag.textContent = detail.flagged ? "Flagged" : "Flag for review";
    }
  });

  /* Time on this question, sent with the next save. Reset per navigation, which
     is per page load, because navigation is a page load. */
  document.body.addEventListener("htmx:configRequest", function (event) {
    if (timeField) {
      event.detail.parameters.time_ms = String(Date.now() - shownAt);
    }
  });

  /* ----------------------------------------------------------------- submitting */

  window.examkbConfirmSubmit = function (form) {
    var left = unanswered ? parseInt(unanswered.textContent, 10) : 0;
    if (!left) return confirm("Submit this exam? Answers cannot be changed afterwards.");
    return confirm(
      left + (left === 1 ? " question is" : " questions are") +
      " still unanswered. Submit anyway? Blank answers score zero."
    );
  };
})();
