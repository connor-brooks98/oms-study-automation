((root) => {
  "use strict";

  const FLAG_REASONS = Object.freeze([
    "inaccurate_question",
    "ambiguous_question",
    "want_to_review",
    "other",
  ]);

  const FLAG_REASON_LABELS = Object.freeze({
    inaccurate_question: "Inaccurate question",
    ambiguous_question: "Ambiguous or unclear",
    want_to_review: "Want to review later",
    other: "Other",
  });

  const questionState = (question) => ({
    choiceIds: question.choices.map((choice) => choice.id),
    selectedChoiceId: null,
    eliminatedChoiceIds: [],
    highlights: [],
    submitted: false,
    feedback: null,
    flagReason: null,
  });

  const createQuizState = (content) => ({
    token: content.token,
    version: content.version,
    currentIndex: 0,
    score: 0,
    questions: Object.fromEntries(
      content.questions.map((question) => [
        question.id,
        questionState(question),
      ]),
    ),
  });

  const updateQuestion = (state, questionId, updater) => {
    const current = state.questions[questionId];
    if (!current) throw new Error(`Unknown question: ${questionId}`);
    const updated = updater(current);
    if (updated === current) return state;
    return {
      ...state,
      questions: {
        ...state.questions,
        [questionId]: updated,
      },
    };
  };

  const selectChoice = (state, questionId, choiceId) => (
    updateQuestion(state, questionId, (question) => {
      if (!question.choiceIds.includes(choiceId)) {
        throw new Error(`Unknown choice: ${choiceId}`);
      }
      if (
        question.submitted
        || question.eliminatedChoiceIds.includes(choiceId)
      ) {
        return question;
      }
      return { ...question, selectedChoiceId: choiceId };
    })
  );

  const toggleEliminated = (state, questionId, choiceId) => (
    updateQuestion(state, questionId, (question) => {
      if (!question.choiceIds.includes(choiceId)) {
        throw new Error(`Unknown choice: ${choiceId}`);
      }
      if (question.submitted) return question;
      const eliminated = question.eliminatedChoiceIds.includes(choiceId);
      return {
        ...question,
        selectedChoiceId: (
          !eliminated && question.selectedChoiceId === choiceId
            ? null
            : question.selectedChoiceId
        ),
        eliminatedChoiceIds: eliminated
          ? question.eliminatedChoiceIds.filter((id) => id !== choiceId)
          : [...question.eliminatedChoiceIds, choiceId],
      };
    })
  );

  const mergedRanges = (ranges) => (
    [...ranges]
      .filter(({ start, end }) => (
        Number.isInteger(start) && Number.isInteger(end) && start < end
      ))
      .sort((left, right) => left.start - right.start)
      .reduce((result, range) => {
        const last = result.at(-1);
        if (last && range.start <= last.end) {
          last.end = Math.max(last.end, range.end);
        } else {
          result.push({ start: range.start, end: range.end });
        }
        return result;
      }, [])
  );

  const addHighlight = (state, questionId, start, end) => (
    updateQuestion(state, questionId, (question) => ({
      ...question,
      highlights: mergedRanges([
        ...question.highlights,
        { start, end },
      ]),
    }))
  );

  const clearHighlights = (state, questionId) => (
    updateQuestion(state, questionId, (question) => ({
      ...question,
      highlights: [],
    }))
  );

  const setFlagReason = (state, questionId, reason) => {
    const normalized = reason || null;
    if (normalized !== null && !FLAG_REASONS.includes(normalized)) {
      throw new Error(`Unknown flag reason: ${reason}`);
    }
    return updateQuestion(state, questionId, (question) => ({
      ...question,
      flagReason: normalized,
    }));
  };

  const navigateQuestion = (state, index, totalQuestions) => {
    if (
      !Number.isInteger(index)
      || !Number.isInteger(totalQuestions)
      || totalQuestions < 1
      || index < 0
      || index > totalQuestions
    ) {
      throw new Error("Question index is out of range.");
    }
    return { ...state, currentIndex: index };
  };

  const performanceSummary = (content, state) => {
    const dimensions = {
      areas: "area",
      objectives: "learning_objective",
      topics: "topic",
    };
    const groups = Object.fromEntries(
      Object.keys(dimensions).map((name) => [name, new Map()]),
    );
    let correct = 0;
    let answered = 0;
    let flagged = 0;
    for (const question of content.questions) {
      const progress = state.questions[question.id];
      const submitted = Boolean(
        progress?.submitted && progress.feedback,
      );
      const isCorrect = submitted && progress.feedback.correct === true;
      const isFlagged = Boolean(progress?.flagReason);
      if (submitted) answered += 1;
      if (isCorrect) correct += 1;
      if (isFlagged) flagged += 1;
      for (const [name, field] of Object.entries(dimensions)) {
        const value = question[field] || (
          field === "learning_objective" ? question.objective : null
        );
        const label = value || (
          field === "topic" && content.topic ? content.topic : "General"
        );
        const current = groups[name].get(label) || {
          label,
          total: 0,
          answered: 0,
          correct: 0,
          incorrect: 0,
          unanswered: 0,
          needReview: 0,
          flagged: 0,
        };
        current.total += 1;
        if (isFlagged) current.flagged += 1;
        if (submitted) {
          current.answered += 1;
          if (isCorrect) current.correct += 1;
          else {
            current.incorrect += 1;
            current.needReview += 1;
          }
        } else {
          current.unanswered += 1;
          current.needReview += 1;
        }
        groups[name].set(label, current);
      }
    }
    const total = content.questions.length;
    return {
      total,
      answered,
      correct,
      incorrect: answered - correct,
      unanswered: total - answered,
      flagged,
      percentage: total ? Math.round((correct / total) * 100) : 0,
      areas: [...groups.areas.values()],
      objectives: [...groups.objectives.values()],
      topics: [...groups.topics.values()],
    };
  };

  const recordFeedback = (state, questionId, feedback) => {
    const current = state.questions[questionId];
    if (!current) throw new Error(`Unknown question: ${questionId}`);
    if (current.submitted) return state;
    if (!current.selectedChoiceId) {
      throw new Error("Choose an answer before submitting.");
    }
    const next = updateQuestion(state, questionId, (question) => ({
      ...question,
      submitted: true,
      feedback,
    }));
    return {
      ...next,
      score: state.score + (feedback.correct ? 1 : 0),
    };
  };

  const serializeProgress = (state) => JSON.stringify(state);

  const restoreProgress = (content, serialized) => {
    const fresh = createQuizState(content);
    if (!serialized) return fresh;
    try {
      const saved = JSON.parse(serialized);
      if (
        saved.token !== content.token
        || saved.version !== content.version
        || !Number.isInteger(saved.currentIndex)
        || saved.currentIndex < 0
        || saved.currentIndex > content.questions.length
      ) {
        return fresh;
      }
      const restoredQuestions = {};
      for (const question of content.questions) {
        const baseline = fresh.questions[question.id];
        const candidate = saved.questions?.[question.id];
        if (!candidate) return fresh;
        const validChoices = new Set(baseline.choiceIds);
        if (
          candidate.selectedChoiceId !== null
          && !validChoices.has(candidate.selectedChoiceId)
        ) {
          return fresh;
        }
        const eliminated = Array.isArray(candidate.eliminatedChoiceIds)
          ? candidate.eliminatedChoiceIds.filter((id) => validChoices.has(id))
          : [];
        const submitted = candidate.submitted === true;
        const feedbackValid = (
          candidate.feedback
          && typeof candidate.feedback.correct === "boolean"
          && validChoices.has(candidate.feedback.correct_choice_id)
          && typeof candidate.feedback.rationale === "string"
          && candidate.feedback.rationale.length > 0
        );
        if (
          submitted
          && (!candidate.selectedChoiceId || !feedbackValid)
        ) {
          return fresh;
        }
        restoredQuestions[question.id] = {
          ...baseline,
          selectedChoiceId: candidate.selectedChoiceId,
          eliminatedChoiceIds: [...new Set(eliminated)],
          highlights: mergedRanges(
            Array.isArray(candidate.highlights) ? candidate.highlights : [],
          ).filter((range) => range.end <= question.stem.length),
          submitted,
          feedback: submitted
            ? candidate.feedback
            : null,
          flagReason: FLAG_REASONS.includes(candidate.flagReason)
            ? candidate.flagReason
            : null,
        };
      }
      return {
        ...fresh,
        currentIndex: saved.currentIndex,
        score: Object.values(restoredQuestions).filter(
          (question) => question.submitted && question.feedback?.correct,
        ).length,
        questions: restoredQuestions,
      };
    } catch {
      return fresh;
    }
  };

  const csrfToken = (documentRef) => {
    const prefix = "study_hub_csrf=";
    const cookie = documentRef.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith(prefix));
    return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : "";
  };

  const answerRequest = async (
    fetchImpl,
    url,
    questionId,
    choiceId,
    csrf,
  ) => {
    const response = await fetchImpl(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
      },
      body: JSON.stringify({
        question_id: questionId,
        choice_id: choiceId,
      }),
      cache: "no-store",
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Your answer could not be submitted.");
    }
    return payload;
  };

  const element = (documentRef, tag, className, text, focusKey) => {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    if (focusKey) node.dataset.focusKey = focusKey;
    return node;
  };

  // Reads the data-focus-key of the currently focused control (if any)
  // inside `container`, so a subsequent re-render can restore focus to
  // the equivalent control instead of silently dropping it to <body>.
  const captureFocusKey = (documentRef, container) => {
    const active = documentRef.activeElement;
    if (
      !active
      || typeof container.contains !== "function"
      || !container.contains(active)
    ) {
      return undefined;
    }
    return active.dataset?.focusKey || null;
  };

  // Restores focus after a re-render: prefers the control that carries the
  // same data-focus-key as whatever was focused before, and falls back to
  // the player container (which must be focusable, e.g. tabindex="-1").
  const restoreFocus = (container, focusKey) => {
    if (focusKey === undefined) return;
    const match = focusKey
      ? container.querySelector(`[data-focus-key="${focusKey}"]`)
      : null;
    // A disabled control can't receive focus — calling .focus() on one is a
    // silent no-op in real browsers, which would leave focus wherever it
    // landed when the old node was removed (typically <body>). Fall back to
    // the (focusable, tabindex="-1") container in that case.
    (match && !match.disabled ? match : container).focus();
  };

  const renderHighlightedText = (
    documentRef,
    container,
    text,
    ranges,
  ) => {
    let position = 0;
    for (const range of mergedRanges(ranges)) {
      if (range.start > position) {
        container.append(documentRef.createTextNode(
          text.slice(position, range.start),
        ));
      }
      const mark = element(
        documentRef,
        "mark",
        "quiz-highlight",
        text.slice(range.start, range.end),
      );
      container.append(mark);
      position = range.end;
    }
    if (position < text.length) {
      container.append(documentRef.createTextNode(text.slice(position)));
    }
  };

  const selectionOffsets = (container, selection) => {
    if (!selection || selection.rangeCount !== 1) return null;
    const range = selection.getRangeAt(0);
    if (
      range.collapsed
      || !container.contains(range.commonAncestorContainer)
    ) {
      return null;
    }
    const before = range.cloneRange();
    before.selectNodeContents(container);
    before.setEnd(range.startContainer, range.startOffset);
    const start = before.toString().length;
    return { start, end: start + range.toString().length };
  };

  const storageKey = (content) => (
    `oms-study-hub-quiz:${content.token}:v${content.version}`
  );

  const initialize = async (
    documentRef,
    fetchImpl = root.fetch.bind(root),
  ) => {
    const app = documentRef.querySelector("[data-quiz-token]");
    if (!app) return;
    const allowUnansweredNavigation = (
      app.dataset.allowUnansweredNavigation === "true"
    );
    try {
      const response = await fetchImpl(app.dataset.contentUrl, {
        cache: "no-store",
      });
      if (!response.ok) {
        app.textContent = "This quiz could not be loaded.";
        return;
      }
      const content = await response.json();
      const storage = documentRef.defaultView?.localStorage;
      const key = storageKey(content);
      let state = restoreProgress(content, storage?.getItem(key));

      const persist = () => {
        storage?.setItem(key, serializeProgress(state));
      };

      const render = () => {
        const focusKey = captureFocusKey(documentRef, app);
        app.replaceChildren();
        if (state.currentIndex >= content.questions.length) {
          const summary = performanceSummary(content, state);
          const result = element(documentRef, "section", "quiz-result");
          result.append(
            element(documentRef, "p", "quiz-brand", "Study Hub"),
            element(documentRef, "h1", "sh-title", "Quiz complete"),
            element(
              documentRef,
              "p",
              "quiz-score",
              `${summary.correct} / ${summary.total}`,
            ),
            element(
              documentRef,
              "p",
              "quiz-result-copy",
              `${summary.percentage}% correct · Your answers were stored only in this browser.`,
            ),
          );
          const summaryHeading = element(
            documentRef,
            "h2",
            "quiz-summary-heading sh-h2",
            "Performance summary",
          );
          result.append(summaryHeading);
          const summaryGrid = element(documentRef, "div", "quiz-summary-grid");
          for (const [title, key] of [
            ["Areas", "areas"],
            ["Learning objectives", "objectives"],
            ["Topics", "topics"],
          ]) {
            const group = element(documentRef, "section", "quiz-summary-group");
            group.append(element(documentRef, "h3", "", title));
            const table = element(documentRef, "table", "quiz-summary-table");
            const head = element(documentRef, "thead");
            const headRow = element(documentRef, "tr");
            for (const heading of ["Item", "Right", "Need review", "Flagged"]) {
              headRow.append(element(documentRef, "th", "", heading));
            }
            head.append(headRow);
            const body = element(documentRef, "tbody");
            for (const row of summary[key]) {
              const tableRow = element(documentRef, "tr");
              tableRow.append(
                element(documentRef, "th", "", row.label),
                element(documentRef, "td", "", `${row.correct} / ${row.total}`),
                element(documentRef, "td", "", String(row.needReview)),
                element(documentRef, "td", "", String(row.flagged)),
              );
              body.append(tableRow);
            }
            table.append(head, body);
            group.append(table);
            summaryGrid.append(group);
          }
          result.append(summaryGrid);
          const review = element(
            documentRef,
            "button",
            "quiz-secondary quiz-review sh-btn sh-btn--secondary",
            "Review answers",
            "result-review",
          );
          review.type = "button";
          review.addEventListener("click", () => {
            state = navigateQuestion(state, 0, content.questions.length);
            persist();
            render();
          });
          result.append(review);
          app.append(result);
          restoreFocus(app, focusKey);
          return;
        }

        const question = content.questions[state.currentIndex];
        const questionProgress = state.questions[question.id];
        const shell = element(documentRef, "article", "quiz-shell");
        const header = element(documentRef, "header", "quiz-header");
        const meta = element(documentRef, "div", "quiz-meta");
        const context = [
          content.course,
          content.exam_number != null ? `Exam ${content.exam_number}` : null,
          content.lecture_number != null
            ? `Lecture ${content.lecture_number}`
            : null,
          content.topic,
        ].filter(Boolean).join(" · ");
        meta.append(
          element(
            documentRef,
            "span",
            "quiz-course",
            context || "Study Hub quiz",
          ),
          element(
            documentRef,
            "span",
            "quiz-counter",
            `Question ${state.currentIndex + 1} of ${content.questions.length}`,
          ),
        );
        const track = element(documentRef, "div", "quiz-progress");
        track.setAttribute("role", "progressbar");
        track.setAttribute("aria-label", "Quiz progress");
        track.setAttribute("aria-valuemin", "0");
        track.setAttribute("aria-valuemax", String(content.questions.length));
        track.setAttribute("aria-valuenow", String(state.currentIndex + 1));
        const fill = element(documentRef, "span", "quiz-progress-fill");
        fill.style.width = (
          `${((state.currentIndex + 1) / content.questions.length) * 100}%`
        );
        track.append(fill);
        header.append(meta, track);

        const body = element(documentRef, "div", "quiz-body");
        body.append(
          element(documentRef, "p", "quiz-label", content.topic),
        );
        const stem = element(documentRef, "p", "quiz-question");
        renderHighlightedText(
          documentRef,
          stem,
          question.stem,
          questionProgress.highlights,
        );
        body.append(stem);

        if (question.image_url) {
          const figure = element(documentRef, "figure", "quiz-question-image");
          const image = element(documentRef, "img");
          image.src = question.image_url;
          image.alt = question.image_alt || "Question source image";
          image.loading = "lazy";
          if (question.image_width && question.image_height) {
            image.width = question.image_width;
            image.height = question.image_height;
          }
          figure.append(image);
          body.append(figure);
        }

        const tools = element(documentRef, "div", "quiz-tools");
        const highlight = element(
          documentRef,
          "button",
            "quiz-tool sh-btn sh-btn--ghost sh-btn--sm",
          "Highlight selection",
          "tool-highlight",
        );
        highlight.type = "button";
        highlight.addEventListener("click", () => {
          const offsets = selectionOffsets(
            stem,
            documentRef.getSelection(),
          );
          if (!offsets) return;
          state = addHighlight(
            state,
            question.id,
            offsets.start,
            offsets.end,
          );
          persist();
          render();
        });
        const clear = element(
          documentRef,
          "button",
            "quiz-tool sh-btn sh-btn--secondary sh-btn--sm",
          "Clear highlights",
          "tool-clear",
        );
        clear.type = "button";
        clear.disabled = questionProgress.highlights.length === 0;
        clear.addEventListener("click", () => {
          state = clearHighlights(state, question.id);
          persist();
          render();
        });
        tools.append(highlight, clear);
        body.append(tools);

        const flag = element(documentRef, "div", "quiz-flag");
        const flagLabel = element(documentRef, "label", "quiz-flag-label", "Flag this question");
        const flagSelect = documentRef.createElement("select");
        flagSelect.className = "quiz-flag-select sh-select";
        flagSelect.dataset.focusKey = "flag-select";
        flagSelect.setAttribute("aria-label", "Reason for flagging this question");
        const noFlag = element(documentRef, "option", "", "No flag");
        noFlag.value = "";
        flagSelect.append(noFlag);
        for (const reason of FLAG_REASONS) {
          const option = element(
            documentRef,
            "option",
            "",
            FLAG_REASON_LABELS[reason],
          );
          option.value = reason;
          flagSelect.append(option);
        }
        flagSelect.value = questionProgress.flagReason || "";
        flagSelect.addEventListener("change", () => {
          state = setFlagReason(state, question.id, flagSelect.value);
          persist();
        });
        flagLabel.append(flagSelect);
        flag.append(flagLabel);
        body.append(flag);

        const answers = element(documentRef, "div", "quiz-answers");
        for (const [index, choice] of question.choices.entries()) {
          const selected = questionProgress.selectedChoiceId === choice.id;
          const eliminated = questionProgress.eliminatedChoiceIds.includes(
            choice.id,
          );
          const correct = (
            questionProgress.submitted
            && questionProgress.feedback.correct_choice_id === choice.id
          );
          const incorrect = (
            questionProgress.submitted
            && selected
            && !questionProgress.feedback.correct
          );
          const row = element(documentRef, "div", "quiz-answer-row sh-option");
          if (selected) row.classList.add("is-selected", "sh-option--selected");
          if (eliminated) row.classList.add("is-eliminated");
          if (correct) row.classList.add("is-correct", "sh-option--correct");
          if (incorrect) row.classList.add("is-incorrect", "sh-option--incorrect");

          const answer = element(
            documentRef,
            "button",
            "quiz-answer",
            undefined,
            `answer-${choice.id}`,
          );
          answer.type = "button";
          answer.disabled = questionProgress.submitted;
          answer.setAttribute("aria-pressed", String(selected));
          answer.append(
            element(
              documentRef,
              "span",
              "quiz-choice-letter sh-option__medallion",
              String.fromCharCode(65 + index),
            ),
            element(documentRef, "span", "quiz-choice-text", choice.text),
          );
          answer.addEventListener("click", () => {
            state = selectChoice(state, question.id, choice.id);
            persist();
            render();
          });

          const strike = element(
            documentRef,
            "button",
            "quiz-strike sh-iconbtn",
            "✕",
            `strike-${choice.id}`,
          );
          strike.type = "button";
          strike.disabled = questionProgress.submitted;
          strike.setAttribute("aria-pressed", String(eliminated));
          strike.setAttribute(
            "aria-label",
            `${eliminated ? "Restore" : "Cross out"} answer ${String.fromCharCode(65 + index)}`,
          );
          strike.title = eliminated ? "Restore answer" : "Cross out answer";
          strike.addEventListener("click", () => {
            state = toggleEliminated(state, question.id, choice.id);
            persist();
            render();
          });
          row.append(answer, strike);
          answers.append(row);
        }
        body.append(answers);

        const feedback = element(documentRef, "section", "quiz-feedback");
        feedback.setAttribute("role", "status");
        feedback.setAttribute("aria-live", "polite");
        if (questionProgress.submitted) {
          feedback.classList.add(
            questionProgress.feedback.correct ? "is-correct" : "is-incorrect",
          );
          feedback.append(
            element(
              documentRef,
              "h2",
              "",
              questionProgress.feedback.correct
                ? "Correct"
                : "Review this answer",
            ),
            element(
              documentRef,
              "p",
              "quiz-feedback-label",
              "Expert rationale",
            ),
            element(
              documentRef,
              "p",
              "",
              questionProgress.feedback.rationale,
            ),
          );
        } else {
          feedback.hidden = true;
        }
        body.append(feedback);

        if (!questionProgress.submitted) {
          body.append(
            element(
              documentRef,
              "p",
              "quiz-submit-note",
              "You can change your selection until you submit.",
            ),
          );
          const submit = element(
            documentRef,
            "button",
            "quiz-primary quiz-submit sh-btn sh-btn--primary sh-btn--block",
            "Submit Answer",
            "submit",
          );
          submit.type = "button";
          submit.disabled = !questionProgress.selectedChoiceId;
          submit.addEventListener("click", async () => {
            submit.disabled = true;
            submit.textContent = "Checking…";
            try {
              const feedbackResult = await answerRequest(
                fetchImpl,
                app.dataset.answerUrl,
                question.id,
                questionProgress.selectedChoiceId,
                csrfToken(documentRef),
              );
              state = recordFeedback(state, question.id, feedbackResult);
              persist();
              render();
            } catch (error) {
              submit.disabled = false;
              submit.textContent = "Submit Answer";
              const message = element(
                documentRef,
                "p",
                "quiz-error",
                error.message,
              );
              message.setAttribute("role", "alert");
              submit.before(message);
            }
          });
          body.append(submit);
        }
        const navigation = element(
          documentRef,
          "nav",
          "quiz-navigation quiz-navigation-card",
        );
        navigation.setAttribute("aria-label", "Quiz question navigation");
        const back = element(
          documentRef,
          "button",
          "quiz-secondary sh-btn sh-btn--secondary",
          "← Back",
          "back",
        );
        back.type = "button";
        back.disabled = state.currentIndex === 0;
        back.addEventListener("click", () => {
          state = navigateQuestion(state, state.currentIndex - 1, content.questions.length);
          persist();
          render();
        });
        const forward = element(
          documentRef,
          "button",
          "quiz-secondary sh-btn sh-btn--secondary",
          state.currentIndex === content.questions.length - 1
            ? "See results →"
            : "Next →",
          "forward",
        );
        forward.type = "button";
        forward.disabled = !(allowUnansweredNavigation || questionProgress.submitted);
        forward.addEventListener("click", () => {
          if (!(allowUnansweredNavigation || questionProgress.submitted)) return;
          state = navigateQuestion(state, state.currentIndex + 1, content.questions.length);
          persist();
          render();
        });
        navigation.append(back, forward);

        const dimensions = [
          ["Area", question.area],
          ["Objective", question.learning_objective || question.objective],
          ["Topic", question.topic],
        ].filter(([, value]) => value);
        if (dimensions.length > 0) {
          const information = element(
            documentRef,
            "details",
            "quiz-information",
          );
          const summary = element(
            documentRef,
            "summary",
            "quiz-information-summary",
            "Question Information",
          );
          const tags = element(documentRef, "div", "quiz-dimensions");
          for (const [label, value] of dimensions) {
            tags.append(element(documentRef, "span", "quiz-dimension", `${label}: ${value}`));
          }
          information.append(summary, tags);
          body.append(information);
        }
        shell.append(header, body);
        app.append(navigation, shell);
        restoreFocus(app, focusKey);
      };

      render();
    } catch {
      app.textContent = "This quiz could not be loaded.";
    }
  };

  const api = {
    FLAG_REASONS,
    addHighlight,
    answerRequest,
    captureFocusKey,
    clearHighlights,
    createQuizState,
    initialize,
    navigateQuestion,
    performanceSummary,
    recordFeedback,
    restoreFocus,
    restoreProgress,
    selectChoice,
    setFlagReason,
    serializeProgress,
    toggleEliminated,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document) {
    if (root.document.readyState === "loading") {
      root.document.addEventListener(
        "DOMContentLoaded",
        () => void initialize(root.document),
        { once: true },
      );
    } else {
      void initialize(root.document);
    }
  }
})(typeof globalThis === "undefined" ? this : globalThis);
