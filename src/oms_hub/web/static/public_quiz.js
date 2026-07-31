((root) => {
  "use strict";

  const questionState = (question) => ({
    choiceIds: question.choices.map((choice) => choice.id),
    selectedChoiceId: null,
    eliminatedChoiceIds: [],
    highlights: [],
    submitted: false,
    submitting: false,
    submissionError: null,
    feedback: null,
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
        || question.submitting
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
      if (question.submitted || question.submitting) return question;
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

  const beginSubmission = (state, questionId) => (
    updateQuestion(state, questionId, (question) => {
      if (question.submitted || question.submitting) return question;
      if (!question.selectedChoiceId) {
        throw new Error("Choose an answer before submitting.");
      }
      return {
        ...question,
        submitting: true,
        submissionError: null,
      };
    })
  );

  const cancelSubmission = (state, questionId, message) => (
    updateQuestion(state, questionId, (question) => ({
      ...question,
      submitting: false,
      submissionError: message,
    }))
  );

  const recordFeedback = (
    state,
    questionId,
    feedback,
    gradedChoiceId = null,
  ) => {
    const current = state.questions[questionId];
    if (!current) throw new Error(`Unknown question: ${questionId}`);
    if (current.submitted) return state;
    const selectedChoiceId = gradedChoiceId || current.selectedChoiceId;
    if (!selectedChoiceId || !current.choiceIds.includes(selectedChoiceId)) {
      throw new Error("Choose an answer before submitting.");
    }
    const next = updateQuestion(state, questionId, (question) => ({
      ...question,
      selectedChoiceId,
      submitted: true,
      submitting: false,
      submissionError: null,
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

  const safeJson = async (response) => {
    try {
      return await response.json();
    } catch (_error) {
      return {};
    }
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
    const payload = await safeJson(response);
    if (!response.ok) {
      throw new Error(payload.detail || "Your answer could not be submitted.");
    }
    if (
      typeof payload.correct !== "boolean"
      || typeof payload.correct_choice_id !== "string"
      || typeof payload.rationale !== "string"
    ) {
      throw new Error("Your answer could not be submitted.");
    }
    return payload;
  };

  const loadQuizContent = async (fetchImpl, url) => {
    const response = await fetchImpl(url, { cache: "no-store" });
    const payload = await safeJson(response);
    if (!response.ok || !Array.isArray(payload.questions)) {
      throw new Error(
        payload.detail || "This quiz could not be loaded.",
      );
    }
    return payload;
  };

  const acquireStorage = (view) => {
    try {
      return view?.localStorage || null;
    } catch (_error) {
      return null;
    }
  };

  const persistProgress = (storage, key, state) => {
    try {
      storage?.setItem(key, serializeProgress(state));
      return true;
    } catch (_error) {
      return false;
    }
  };

  const choiceResultLabel = (correct, incorrect) => {
    if (correct) return "✓ Correct";
    if (incorrect) return "✗ Your answer";
    return "";
  };

  const element = (documentRef, tag, className, text) => {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const quizMetadata = (content) => {
    const parts = [`${content.course} · Exam ${content.exam_number}`];
    if (Number.isInteger(content.lecture_number)) {
      parts.push(`Lecture ${content.lecture_number}`);
    }
    return parts.join(" · ");
  };

  const renderQuestionImage = (documentRef, question) => {
    const url = question?.image_url;
    if (
      typeof url !== "string"
      || !url.startsWith("/")
      || url.startsWith("//")
    ) {
      return null;
    }
    const figure = element(documentRef, "figure", "quiz-question-media");
    const link = element(documentRef, "a", "quiz-question-image-link");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.setAttribute("aria-label", "Open the question image at full size");
    const image = element(documentRef, "img", "quiz-question-image");
    image.src = url;
    image.alt = (
      typeof question.image_alt === "string" && question.image_alt.trim()
        ? question.image_alt
        : "Image used for this question"
    );
    image.loading = "eager";
    image.decoding = "async";
    link.append(image);
    figure.append(link);
    return figure;
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
    let content;
    try {
      content = await loadQuizContent(fetchImpl, app.dataset.contentUrl);
    } catch (_error) {
      app.textContent = "This quiz could not be loaded.";
      return;
    }
    const storage = acquireStorage(documentRef.defaultView);
    const key = storageKey(content);
    let savedProgress = null;
    try {
      savedProgress = storage?.getItem(key) || null;
    } catch (_error) {
      savedProgress = null;
    }
    let state = restoreProgress(content, savedProgress);

    const persist = () => {
      persistProgress(storage, key, state);
    };

    const render = () => {
      app.replaceChildren();
      if (state.currentIndex >= content.questions.length) {
        const result = element(documentRef, "section", "quiz-result");
        result.append(
          element(documentRef, "p", "quiz-brand", "Study Hub"),
          element(documentRef, "h1", "", "Quiz complete"),
          element(
            documentRef,
            "p",
            "quiz-score",
            `${state.score} / ${content.questions.length}`,
          ),
          element(
            documentRef,
            "p",
            "quiz-result-copy",
            "Your answers were stored only in this browser.",
          ),
        );
        const restart = element(
          documentRef,
          "button",
          "quiz-primary",
          "Start Over",
        );
        restart.type = "button";
        restart.addEventListener("click", () => {
          try {
            storage?.removeItem(key);
          } catch (_error) {
            // The quiz can restart even when browser storage is unavailable.
          }
          state = createQuizState(content);
          render();
        });
        result.append(restart);
        app.append(result);
        return;
      }

      const question = content.questions[state.currentIndex];
      const questionProgress = state.questions[question.id];
      const shell = element(documentRef, "article", "quiz-shell");
      const header = element(documentRef, "header", "quiz-header");
      const meta = element(documentRef, "div", "quiz-meta");
      meta.append(
        element(
          documentRef,
          "span",
          "quiz-course",
          quizMetadata(content),
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
      const questionImage = renderQuestionImage(documentRef, question);
      if (questionImage) body.append(questionImage);
      const stem = element(documentRef, "p", "quiz-question");
      renderHighlightedText(
        documentRef,
        stem,
        question.stem,
        questionProgress.highlights,
      );
      body.append(stem);

      const tools = element(documentRef, "div", "quiz-tools");
      const highlight = element(
        documentRef,
        "button",
        "quiz-tool",
        "Highlight selection",
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
        "quiz-tool",
        "Clear highlights",
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
        const row = element(documentRef, "div", "quiz-answer-row");
        if (selected) row.classList.add("is-selected");
        if (eliminated) row.classList.add("is-eliminated");
        if (correct) row.classList.add("is-correct");
        if (incorrect) row.classList.add("is-incorrect");

        const answer = element(documentRef, "button", "quiz-answer");
        answer.type = "button";
        answer.disabled = (
          questionProgress.submitted || questionProgress.submitting
        );
        answer.setAttribute("aria-pressed", String(selected));
        answer.append(
          element(
            documentRef,
            "span",
            "quiz-choice-letter",
            String.fromCharCode(65 + index),
          ),
          element(documentRef, "span", "quiz-choice-text", choice.text),
        );
        if (correct || incorrect) {
          const resultText = choiceResultLabel(correct, incorrect);
          answer.append(
            element(
              documentRef,
              "span",
              "quiz-choice-result",
              resultText,
            ),
          );
          answer.setAttribute(
            "aria-label",
            `Answer ${String.fromCharCode(65 + index)}: ${choice.text}. ${resultText}`,
          );
        }
        answer.addEventListener("click", () => {
          state = selectChoice(state, question.id, choice.id);
          persist();
          render();
        });

        const strike = element(
          documentRef,
          "button",
          "quiz-strike",
          "S",
        );
        strike.type = "button";
        strike.disabled = (
          questionProgress.submitted || questionProgress.submitting
        );
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

      if (questionProgress.submitted) {
        const feedback = element(
          documentRef,
          "section",
          questionProgress.feedback.correct
            ? "quiz-feedback is-correct"
            : "quiz-feedback is-incorrect",
        );
        feedback.setAttribute("aria-live", "polite");
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
        body.append(feedback);
        const continueButton = element(
          documentRef,
          "button",
          "quiz-primary quiz-submit",
          state.currentIndex === content.questions.length - 1
            ? "See Results"
            : "Continue",
        );
        continueButton.type = "button";
        continueButton.addEventListener("click", () => {
          state = { ...state, currentIndex: state.currentIndex + 1 };
          persist();
          render();
          app.focus();
        });
        body.append(continueButton);
      } else {
        if (questionProgress.submissionError) {
          const message = element(
            documentRef,
            "p",
            "quiz-error",
            questionProgress.submissionError,
          );
          message.setAttribute("role", "alert");
          body.append(message);
        }
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
          "quiz-primary quiz-submit",
          questionProgress.submitting ? "Checking…" : "Submit Answer",
        );
        submit.type = "button";
        submit.disabled = (
          !questionProgress.selectedChoiceId || questionProgress.submitting
        );
        submit.addEventListener("click", async () => {
          const gradedChoiceId = state.questions[question.id].selectedChoiceId;
          const next = beginSubmission(state, question.id);
          if (next === state) return;
          state = next;
          persist();
          render();
          try {
            const feedback = await answerRequest(
              fetchImpl,
              app.dataset.answerUrl,
              question.id,
              gradedChoiceId,
              csrfToken(documentRef),
            );
            state = recordFeedback(
              state,
              question.id,
              feedback,
              gradedChoiceId,
            );
            persist();
            render();
          } catch (error) {
            state = cancelSubmission(
              state,
              question.id,
              error instanceof Error
                ? error.message
                : "Your answer could not be submitted.",
            );
            persist();
            render();
          }
        });
        body.append(submit);
      }
      shell.append(header, body);
      app.append(shell);
    };

    render();
  };

  const api = {
    addHighlight,
    acquireStorage,
    answerRequest,
    beginSubmission,
    cancelSubmission,
    choiceResultLabel,
    clearHighlights,
    createQuizState,
    initialize,
    loadQuizContent,
    persistProgress,
    recordFeedback,
    renderQuestionImage,
    restoreProgress,
    selectChoice,
    serializeProgress,
    toggleEliminated,
    quizMetadata,
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
