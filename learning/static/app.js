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
  const lesson = await api.get("/api/lesson/" + encodeURIComponent(lessonId));
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
      if (state.hintIndex >= lesson.hint_count) return;
      const { data } = await api.post("/api/hint", { lesson_id: lesson.id, index: state.hintIndex });
      if (data.text) {
        state.hintIndex += 1;
        hintZone.append(el("div", { class: "hint-box" }, el("b", {}, `Hint ${data.level}: `), data.text));
        hintBtn.textContent = state.hintIndex < lesson.hint_count
          ? `Hint (${state.hintIndex + 1}/${lesson.hint_count})` : "No more hints";
        if (state.hintIndex >= lesson.hint_count) hintBtn.disabled = true;
      }
    });
    if (state.hintIndex >= lesson.hint_count) { hintBtn.disabled = true; hintBtn.textContent = "No more hints"; }
    view.append(el("div", { class: "block" }, el("div", { class: "block-kind" }, "Stuck?"), hintBtn, hintZone));
  }

  // footer: complete / continue
  const footer = el("div", { class: "lesson-footer" });
  const continueBtn = el("button", { class: "primary" }, "Continue");
  const missingBox = el("div", { class: "missing-list" });
  continueBtn.addEventListener("click", async () => {
    const { ok, data } = await api.post("/api/complete", { lesson_id: lesson.id });
    if (!ok) {
      missingBox.innerHTML = "";
      missingBox.append(el("div", { class: "feedback-fail" },
        el("b", {}, "Not complete yet: "), (data.missing || []).join("; ")));
      return;
    }
    loadLesson(data.next_lesson_id);
    window.scrollTo(0, 0);
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
    btn.addEventListener("click", async () => {
      await api.post("/api/step", { lesson_id: lesson.id, block_id: block.id, kind: "learn" });
      btn.textContent = "✓ Noted"; btn.disabled = true; box.classList.add("done");
    });
    box.append(btn);
  }

  if (block.type === "predict" || block.type === "quiz") {
    box.append(paragraphs(block.question));
    const options = el("div", { class: "options" });
    const feedback = el("div");
    const alreadyCorrect = lesson.progress.quiz_correct.includes(block.id);
    block.options.forEach((option, index) => {
      const opt = el("button", {}, mdInline(option));
      if (alreadyCorrect && index === block.answer_index) opt.classList.add("chosen-correct");
      opt.addEventListener("click", async () => {
        const { data } = await api.post("/api/step", {
          lesson_id: lesson.id, block_id: block.id, kind: block.type, answer_index: index,
        });
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
    btn.addEventListener("click", async () => {
      await api.post("/api/step", { lesson_id: lesson.id, block_id: block.id, kind: "do" });
      btn.textContent = "✓ Done"; btn.disabled = true; box.classList.add("done");
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
    runBtn.addEventListener("click", async () => {
      runBtn.disabled = true;
      runBtn.textContent = "Running…";
      resultZone.innerHTML = "";
      const { data } = await api.post("/api/validate", { lesson_id: lesson.id, block_id: block.id });
      runBtn.disabled = false;
      runBtn.textContent = "Run check";
      if (data.error) {
        resultZone.append(el("div", { class: "feedback-fail" }, data.error));
        return;
      }
      resultZone.append(renderCheckResult(data));
      if (data.passed) {
        box.classList.add("done");
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
    btn.addEventListener("click", async () => {
      const answer = area.value.trim();
      if (!answer) return;
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

async function nextInterviewQuestion() {
  const data = await api.get("/api/interview");
  interviewCurrent = data;
  document.getElementById("interview-question").textContent = data.question;
  document.getElementById("interview-answer").value = "";
  document.getElementById("interview-answer").disabled = false;
  document.getElementById("interview-submit").disabled = false;
  document.getElementById("interview-feedback").innerHTML = "";
}

async function submitInterviewAnswer() {
  if (!interviewCurrent) return;
  const answer = document.getElementById("interview-answer").value.trim();
  if (!answer) return;
  const { data } = await api.post("/api/interview/answer", {
    question_id: interviewCurrent.id, answer_text: answer,
  });
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
