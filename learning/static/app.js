/* flight-test-lab learning client — thin renderer; semantics live server-side */
"use strict";

const api = {
  async get(path) {
    const res = await fetch(path);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  },
  async post(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return { ok: res.ok, status: res.status, data };
  },
};

const state = {
  lesson: null,        // current lesson payload (includes progress)
  hintIndex: 0,        // next hint to request for current lesson
  navigation: 0,       // bumped per lesson load; stale responses are discarded
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    node.append(child);
  }
  return node;
}

// Every click that changes server state goes through this. Handlers that
// awaited before disabling their control let a double-click post twice, and
// the server counts both: record_quiz bumps quiz_attempts and concept mastery
// per request, so one choice could be scored several times. The control is
// disabled *before* the request and re-enabled afterwards unless the handler
// returns true to keep it disabled.
function guardedClick(control, handler) {
  let inFlight = false;
  control.addEventListener("click", async () => {
    if (inFlight || control.disabled) return;
    inFlight = true;
    control.disabled = true;
    let keepDisabled = false;
    try {
      keepDisabled = await handler();
    } finally {
      inFlight = false;
      if (!keepDisabled) control.disabled = false;
    }
  });
}

function mdInline(text) {
  // Minimal inline formatting: `code` and **bold**. Content is trusted (repo curriculum).
  return String(text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
}

function paragraphs(text) {
  const frag = document.createDocumentFragment();
  for (const chunk of String(text).split(/\n{2,}/)) {
    if (chunk.startsWith("```")) {
      // A language tag is a lone word on the fence line: ```python\n...
      // Anything else after the fence is the first line of code (```python -m ...),
      // so only strip the tag when a newline follows it — never eat code.
      const code = chunk.replace(/^```[\w.+-]*\n/, "").replace(/^```/, "").replace(/```$/, "");
      frag.append(el("pre", {}, el("code", {}, code)));
    } else {
      const p = el("p", { html: mdInline(chunk).replace(/\n/g, "<br>") });
      frag.append(p);
    }
  }
  return frag;
}

function switchView(name) {
  for (const view of ["focus", "roadmap", "interview"]) {
    document.getElementById("view-" + view).classList.toggle("hidden", view !== name);
    document.getElementById("nav-" + view).classList.toggle("active", view === name);
  }
  if (name === "roadmap") renderRoadmap();
  if (name === "interview") nextInterviewQuestion();
}

async function refreshTopbar() {
  const data = await api.get("/api/state");
  document.getElementById("program-label").textContent =
    `${data.completed_lessons}/${data.total_lessons} lessons`;
  document.getElementById("program-bar").style.width =
    (data.total_lessons ? (100 * data.completed_lessons) / data.total_lessons : 0) + "%";
  const mastery = document.getElementById("mastery");
  mastery.innerHTML = "";
  const entries = Object.entries(data.mastery).filter(([, m]) => m.correct + m.incorrect > 0);
  mastery.classList.toggle("hidden", entries.length === 0);
  for (const [concept, m] of entries) {
    const cls = m.weak ? "chip weak" : (m.score >= 0.85 ? "chip strong" : "chip");
    mastery.append(el("span", { class: cls, title: `${m.correct} correct, ${m.incorrect} missed` },
      concept));
  }
  return data;
}

/* ---------- focus view ---------- */

async function loadLesson(lessonId) {
  // Two overlapping navigations can finish out of order, and the slower one
  // would then paint the lesson the learner already moved away from. The flow
  // subrequest guards this; the top-level load has to as well.
  const token = ++state.navigation;
  // Opening a lesson is a state change, so it goes through a POST the
  // same-origin guard covers. The GET stays read-only; falling back to it
  // means an unavailable lesson still renders (with its reason) instead of
  // showing nothing.
  const started = await api.post("/api/start", { lesson_id: lessonId });
  const lesson = started.ok
    ? started.data
    : await api.get("/api/lesson/" + encodeURIComponent(lessonId));
  if (token !== state.navigation) return;  // a newer navigation won
  state.lesson = lesson;
  state.hintIndex = lesson.progress.hints_used || 0;
  renderFocus(lesson);
  refreshTopbar();
}

function renderFocus(lesson) {
  const view = document.getElementById("view-focus");
  view.innerHTML = "";

  if (lesson.status === "unavailable") {
    view.append(
      el("div", { class: "lesson-head" },
        el("div", { class: "lesson-kicker" }, `Day ${lesson.day}`),
        el("h1", {}, lesson.title),
        el("p", { class: "objective" }, lesson.objective)),
      el("div", { class: "feedback-warn" },
        el("b", {}, "Not available yet. "),
        lesson.unavailable_reason || "This module depends on tooling that is not set up."),
    );
    return;
  }

  const doneBlocks = new Set([
    ...lesson.progress.steps_done,
    ...lesson.progress.quiz_correct,
    ...lesson.progress.explain_done,
    ...Object.entries(lesson.progress.validations)
      .filter(([, v]) => v.passed).map(([k]) => k),
  ]);

  const head = el("div", { class: "lesson-head" },
    el("div", { class: "lesson-kicker" }, `Day ${lesson.day} — ${lesson.concepts.join(" · ")}`),
    el("h1", {}, lesson.title),
    el("p", { class: "objective" }, lesson.objective),
    el("div", { class: "lesson-meta" },
      el("span", {}, `~${lesson.estimated_minutes} min`),
      lesson.source_files.length
        ? el("span", { class: "mono" }, lesson.source_files.join(", "))
        : "",
    ),
  );
  view.append(head);

  // The flow is fetched asynchronously, so reserve its slot now. Appending
  // from the callback instead would put the diagram after the footer, and a
  // fast lesson switch could drop the previous lesson's flow into this one.
  const flowSlot = lesson.flow ? el("div", { class: "flow-slot" }) : null;
  if (flowSlot) {
    view.append(flowSlot);
    renderFlow(lesson.flow, flowSlot, lesson.id);
  }

  for (const block of lesson.blocks) {
    view.append(renderBlock(lesson, block, doneBlocks.has(block.id)));
  }

  // hints
  if (lesson.hint_count > 0) {
    const hintZone = el("div", { id: "hint-zone" });
    // Re-render hints revealed in an earlier session: progress is advertised
    // as resumable, and a hint you already spent should still be readable.
    for (const hint of lesson.revealed_hints || []) {
      hintZone.append(
        el("div", { class: "hint-box" }, el("b", {}, `Hint ${hint.level}: `), hint.text),
      );
    }
    const hintBtn = el("button", {}, `Hint (${Math.min(state.hintIndex + 1, lesson.hint_count)}/${lesson.hint_count})`);
    hintBtn.addEventListener("click", async () => {
      if (state.hintIndex >= lesson.hint_count || hintBtn.disabled) return;
      // Disabled for the round trip: two clicks landing before the first
      // reply used to advance the counter twice and skip the hint between.
      hintBtn.disabled = true;
      const asked = state.hintIndex;
      const { data } = await api.post("/api/hint", { lesson_id: lesson.id, index: asked });
      if (data.text) {
        // Position comes from the server's revealed count, not from a local
        // increment, so a duplicate reply cannot move it twice.
        state.hintIndex = typeof data.revealed === "number" ? data.revealed : asked + 1;
        hintZone.append(el("div", { class: "hint-box" }, el("b", {}, `Hint ${data.level}: `), data.text));
        hintBtn.textContent = state.hintIndex < lesson.hint_count
          ? `Hint (${state.hintIndex + 1}/${lesson.hint_count})` : "No more hints";
      }
      hintBtn.disabled = state.hintIndex >= lesson.hint_count;
    });
    if (state.hintIndex >= lesson.hint_count) { hintBtn.disabled = true; hintBtn.textContent = "No more hints"; }
    view.append(el("div", { class: "block" }, el("div", { class: "block-kind" }, "Stuck?"), hintBtn, hintZone));
  }

  // footer: complete / continue
  const footer = el("div", { class: "lesson-footer" });
  const continueBtn = el("button", { class: "primary" }, "Continue");
  const missingBox = el("div", { class: "missing-list" });
  guardedClick(continueBtn, async () => {
    const { ok, data } = await api.post("/api/complete", { lesson_id: lesson.id });
    if (!ok) {
      missingBox.innerHTML = "";
      missingBox.append(el("div", { class: "feedback-fail" },
        el("b", {}, "Not complete yet: "), (data.missing || []).join("; ")));
      return false;  // still on this lesson, so let them try again
    }
    loadLesson(data.next_lesson_id);
    window.scrollTo(0, 0);
    return true;  // this view is being replaced
  });
  footer.append(continueBtn);
  if (lesson.progress.complete) {
    footer.append(el("span", { class: "badge pass" }, "LESSON COMPLETE"));
  }
  view.append(footer, missingBox);
}

function renderBlock(lesson, block, done) {
  const box = el("div", { class: "block" + (done ? " done" : ""), id: "block-" + block.id });
  box.append(el("div", { class: "block-kind" }, block.type));

  if (block.type === "learn") {
    box.append(paragraphs(block.text));
    if (block.more) {
      const details = el("details", {},
        el("summary", { class: "muted" }, "Deeper explanation"),
        paragraphs(block.more));
      box.append(details);
    }
    const btn = el("button", {}, done ? "✓ Noted" : "Got it");
    btn.disabled = done;
    guardedClick(btn, async () => {
      await api.post("/api/step", { lesson_id: lesson.id, block_id: block.id, kind: "learn" });
      btn.textContent = "✓ Noted"; box.classList.add("done");
      return true;  // stays acknowledged
    });
    box.append(btn);
  }

  if (block.type === "predict" || block.type === "quiz") {
    box.append(paragraphs(block.question));
    const options = el("div", { class: "options" });
    const feedback = el("div");
    const alreadyCorrect = lesson.progress.quiz_correct.includes(block.id);
    let quizInFlight = false;
    block.options.forEach((option, index) => {
      // html:, not a child — el() appends a string as a text node, so the
      // markup mdInline() produces would show up as literal "<code>ruff
      // format</code>" and "&lt;failure&gt;" in the option text.
      const opt = el("button", { html: mdInline(option) });
      if (alreadyCorrect && index === block.answer_index) opt.classList.add("chosen-correct");
      opt.addEventListener("click", async () => {
        // Guarded across the whole option group, not per button: a second
        // click on any option before the first reply landed posted again, and
        // record_quiz bumps quiz_attempts and concept mastery per request.
        if (quizInFlight || opt.disabled) return;
        quizInFlight = true;
        options.querySelectorAll("button").forEach((b) => (b.disabled = true));
        let data;
        try {
          ({ data } = await api.post("/api/step", {
            lesson_id: lesson.id, block_id: block.id, kind: block.type, answer_index: index,
          }));
        } catch (error) {
          quizInFlight = false;
          options.querySelectorAll("button").forEach((b) => (b.disabled = false));
          throw error;
        }
        quizInFlight = false;
        options.querySelectorAll("button").forEach((b) => b.classList.remove("chosen-wrong"));
        if (data.correct) {
          opt.classList.add("chosen-correct");
          box.classList.add("done");
          feedback.innerHTML = "";
          const note = block.type === "predict" ? data.explanation : (data.explanation || "Correct.");
          if (note) feedback.append(el("div", { class: "feedback-pass", html: mdInline(note) }));
          options.querySelectorAll("button").forEach((b) => (b.disabled = true));
          refreshTopbar();
        } else {
          opt.classList.add("chosen-wrong");
          options.querySelectorAll("button").forEach((b) => (b.disabled = false));
          feedback.innerHTML = "";
          feedback.append(el("div", { class: "feedback-fail" },
            block.type === "quiz"
              ? "Not quite — re-read the material above and try again."
              : "Interesting. Read the reveal, then run the exercise to see for yourself."));
          if (block.type === "predict" && data.explanation) {
            feedback.append(el("div", { class: "reveal", html: mdInline(data.explanation) }));
            box.classList.add("done");
            options.querySelectorAll("button").forEach((b) => (b.disabled = true));
          }
        }
      });
      options.append(opt);
    });
    if (alreadyCorrect) {
      // The live success path disables the options; a reload restored only the
      // highlight, so a stray click on a finished quiz could show a failure
      // and re-record the answer, moving concept mastery for a question the
      // learner had already got right.
      box.classList.add("done");
      options.querySelectorAll("button").forEach((b) => (b.disabled = true));
    }
    box.append(options, feedback);
    if (block.type === "predict" && !alreadyCorrect) {
      // predict blocks don't gate completion; nothing else needed
    }
  }

  if (block.type === "do") {
    box.append(paragraphs(block.instructions));
    if (block.command) box.append(el("pre", {}, el("code", {}, block.command)));
    const btn = el("button", {}, done ? "✓ Done" : "I did this");
    btn.disabled = done;
    guardedClick(btn, async () => {
      await api.post("/api/step", { lesson_id: lesson.id, block_id: block.id, kind: "do" });
      btn.textContent = "✓ Done"; box.classList.add("done");
      return true;  // stays acknowledged
    });
    box.append(btn);
  }

  if (block.type === "verify") {
    if (block.description) box.append(paragraphs(block.description));
    const runBtn = el("button", { class: "primary" }, "Run check");
    const resultZone = el("div");
    if (done) {
      resultZone.append(el("div", { class: "feedback-pass" }, "Check passed" +
        (block.success_note ? " — " + block.success_note : "")));
    }
    guardedClick(runBtn, async () => {
      runBtn.textContent = "Running…";
      resultZone.innerHTML = "";
      const { data } = await api.post("/api/validate", { lesson_id: lesson.id, block_id: block.id });
      runBtn.textContent = "Run check";
      if (data.error) {
        resultZone.append(el("div", { class: "feedback-fail" }, data.error));
        return;
      }
      resultZone.append(renderCheckResult(data));
      if (data.passed) box.classList.add("done");
      // Reloaded on failure too. record_validation revokes a completed
      // lesson's status when a *mandatory* check goes red, so leaving the
      // page as it was would keep the LESSON COMPLETE badge on something the
      // server has already un-completed.
      if (block.mandatory || data.passed) {
        const fresh = await api.get("/api/lesson/" + encodeURIComponent(lesson.id));
        if (state.lesson && state.lesson.id === fresh.id) {
          state.lesson = fresh;
          renderFocus(fresh);
          document.getElementById("block-" + block.id)
            ?.scrollIntoView({ block: "center" });
        }
        refreshTopbar();
      }
    });
    box.append(runBtn, resultZone);
  }

  if (block.type === "explain") {
    box.append(paragraphs(block.question));
    if (done) {
      box.append(el("div", { class: "feedback-pass" }, "Answered."));
      box.append(el("div", { class: "reveal", html: mdInline(block.sample_answer) }));
      return box;
    }
    const area = el("textarea", { placeholder: "Answer in your own words…" });
    const btn = el("button", { class: "primary" }, "Submit");
    const feedback = el("div");
    guardedClick(btn, async () => {
      const answer = area.value.trim();
      if (!answer) return false;
      const { data } = await api.post("/api/step", {
        lesson_id: lesson.id, block_id: block.id, kind: "explain", answer_text: answer,
      });
      feedback.innerHTML = "";
      if (data.passed) {
        box.classList.add("done");
        area.disabled = true; btn.disabled = true;
        feedback.append(el("div", { class: "feedback-pass" }, "Good — key point captured."));
        feedback.append(el("div", { class: "reveal", html: mdInline(data.sample_answer) }));
        refreshTopbar();
      } else {
        feedback.append(el("div", { class: "feedback-fail" },
          "That is missing the key idea. Try once more, then compare with the reference answer."));
        feedback.append(el("div", { class: "reveal", html: mdInline(data.sample_answer) }));
        // allow retry: answer again with the reference visible; resubmission with
        // a keyword still counts — understanding, not gatekeeping
      }
    });
    box.append(area, el("div", { style: "margin-top:6px" }, btn), feedback);
  }

  return box;
}

function renderCheckResult(result) {
  const wrap = el("div", { class: "check-result" });
  const head = el("div", { class: "check-head" },
    el("span", { class: "badge " + (result.passed ? "pass" : "fail") },
      result.passed ? "PASS" : (result.timed_out ? "TIMEOUT" : "FAIL")),
    el("span", { class: "mono" }, result.name),
    el("span", { class: "muted" },
      `exit ${result.exit_status === null ? "—" : result.exit_status} · ${result.duration_ms} ms`),
  );
  wrap.append(head);
  if (result.interpretation) {
    wrap.append(el("div", { class: "check-interpretation" }, result.interpretation));
  }
  if (result.stdout) wrap.append(el("pre", {}, el("code", {}, result.stdout)));
  if (result.stderr) wrap.append(el("pre", {}, el("code", {}, result.stderr)));
  return wrap;
}

async function renderFlow(flowName, container, lessonId) {
  let flow;
  try {
    flow = await api.get("/api/flow/" + encodeURIComponent(flowName));
  } catch {
    return;
  }
  // Discard a response that arrived after the learner moved on: the slot for
  // that lesson is no longer in the document.
  if (!container.isConnected || (lessonId && state.lesson?.id !== lessonId)) {
    return;
  }
  const section = el("div", { class: "block" },
    el("div", { class: "block-kind" }, flow.title || "execution flow"));
  const row = el("div", { class: "flow" });
  const detail = el("div", { class: "flow-detail" });
  flow.stages.forEach((stage, i) => {
    if (i > 0) row.append(el("span", { class: "arrow" }, "→"));
    const node = el("div", { class: "stage" }, stage.name);
    node.addEventListener("click", () => {
      row.querySelectorAll(".stage").forEach((s) => s.classList.remove("selected"));
      node.classList.add("selected");
      detail.innerHTML = "";
      detail.append(
        el("h4", {}, stage.name),
        el("div", { class: "kv", html: mdInline(stage.what || "") }),
        stage.why ? el("div", { class: "kv" }, el("b", {}, "Why it exists: "), stage.why) : "",
        stage.source ? el("div", { class: "kv" }, el("b", {}, "Source: "), el("code", {}, stage.source)) : "",
        stage.concepts ? el("div", { class: "kv" }, el("b", {}, "Concepts: "), stage.concepts) : "",
        stage.failures ? el("div", { class: "kv" }, el("b", {}, "What can fail: "), stage.failures) : "",
      );
    });
    row.append(node);
  });
  section.append(row, detail);
  container.append(section);
}

/* ---------- roadmap ---------- */

async function renderRoadmap() {
  const data = await api.get("/api/curriculum");
  const view = document.getElementById("view-roadmap");
  view.innerHTML = "";
  view.append(el("h2", {}, "14-day program"));
  for (const day of data.days) {
    const details = el("details", { class: "day" });
    const done = day.lessons.filter((l) => l.progress === "complete").length;
    const available = day.lessons.filter((l) => l.status === "available").length;
    details.append(el("summary", {},
      el("span", {}, `Day ${day.day} — ${day.title}`),
      el("span", { class: "day-count" }, available ? `${done}/${available}` : "—"),
    ));
    const body = el("div", { class: "day-body" });
    if (day.summary) body.append(el("p", { class: "muted" }, day.summary));
    for (const lesson of day.lessons) {
      let icon = "○", cls = "status-locked", rowCls = "lesson-row";
      if (lesson.status === "unavailable") { icon = "◌"; cls = "status-unavailable"; rowCls += " unavailable"; }
      else if (lesson.locked) { icon = "🔒"; rowCls += " locked"; }
      else if (lesson.progress === "complete") { icon = "✓"; cls = "status-done"; }
      else if (lesson.progress === "in_progress") { icon = "▸"; cls = "status-current"; }
      const row = el("div", { class: rowCls },
        el("span", { class: "status " + cls }, icon),
        el("span", {}, lesson.title),
        el("span", { class: "est" }, lesson.status === "unavailable"
          ? "unavailable" : `~${lesson.estimated_minutes} min`),
      );
      if (lesson.status === "available" && !lesson.locked) {
        row.addEventListener("click", () => { switchView("focus"); loadLesson(lesson.id); });
      } else if (lesson.status === "unavailable" && lesson.unavailable_reason) {
        row.title = lesson.unavailable_reason;
      }
      body.append(row);
    }
    details.append(body);
    view.append(details);
  }
}

/* ---------- interview ---------- */

let interviewCurrent = null;
// Bumped whenever the displayed question changes. An answer posted for the
// previous question could otherwise return after Next had already loaded a new
// one, render its feedback underneath it, and disable the new answer field.
let interviewToken = 0;

async function nextInterviewQuestion() {
  const token = ++interviewToken;
  const data = await api.get("/api/interview");
  if (token !== interviewToken) return;
  interviewCurrent = data;
  document.getElementById("interview-question").textContent = data.question;
  document.getElementById("interview-answer").value = "";
  document.getElementById("interview-answer").disabled = false;
  document.getElementById("interview-submit").disabled = false;
  document.getElementById("interview-feedback").innerHTML = "";
}

async function submitInterviewAnswer() {
  if (!interviewCurrent) return;
  const submit = document.getElementById("interview-submit");
  // Disabled before the await, not after it. Every click of a double-click
  // reached the request while the button was still live, so one answer was
  // recorded several times — inflating concept mastery and letting a single
  // answer submitted five times satisfy the Day 14 drill's five-answer gate.
  if (submit.disabled) return;
  const answer = document.getElementById("interview-answer").value.trim();
  if (!answer) return;
  submit.disabled = true;
  const token = interviewToken;
  const questionId = interviewCurrent.id;
  let data;
  try {
    ({ data } = await api.post("/api/interview/answer", {
      question_id: questionId, answer_text: answer,
    }));
  } catch (error) {
    submit.disabled = false;  // a failed submission must stay retryable
    throw error;
  }
  // The learner may have pressed Next while this was in flight. The answer is
  // recorded either way — it was a real answer — but its feedback must not
  // land under a different question.
  if (token !== interviewToken) return;
  const feedback = document.getElementById("interview-feedback");
  feedback.innerHTML = "";
  feedback.append(
    el("div", { class: data.correct ? "feedback-pass" : "feedback-fail" },
      data.correct ? "Key point captured." : "Missing the key idea — this one will come back."),
    el("div", { class: "reveal", html: mdInline(data.sample_answer) }),
  );
  document.getElementById("interview-answer").disabled = true;
  document.getElementById("interview-submit").disabled = true;
  refreshTopbar();
}

/* ---------- boot ---------- */

document.getElementById("nav-focus").addEventListener("click", () => switchView("focus"));
document.getElementById("nav-roadmap").addEventListener("click", () => switchView("roadmap"));
document.getElementById("nav-interview").addEventListener("click", () => switchView("interview"));
document.getElementById("interview-submit").addEventListener("click", submitInterviewAnswer);
document.getElementById("interview-next").addEventListener("click", nextInterviewQuestion);

(async () => {
  const data = await refreshTopbar();
  await loadLesson(data.resume_lesson_id);
})();
