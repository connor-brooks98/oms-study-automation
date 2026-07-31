const test = require("node:test");
const assert = require("node:assert/strict");

const images = require("../../src/oms_hub/web/static/studio_quiz_images.js");

const fakeDocument = () => {
  const created = [];
  return {
    created,
    createElement(tagName) {
      const node = {
        tagName,
        textContent: "",
        dataset: {},
        children: [],
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = children; },
      };
      created.push(node);
      return node;
    },
  };
};

test("review renderer groups shared image questions using text-only nodes", () => {
  const documentRef = fakeDocument();
  const container = {
    children: [],
    replaceChildren(...children) { this.children = children; },
    append(child) { this.children.push(child); },
  };
  images.renderReview(documentRef, container, {
    run_id: "run-1",
    resolved: false,
    requirements: [{
      image_key: "image-1",
      source_title: "<img src=x onerror=alert(1)>",
      locator: "Before question 4",
      description: "Questions 4-7",
      uploaded: false,
      width: null,
      height: null,
      original_filename: null,
      questions: [
        { id: "q1", number: 4, stem: "First", overridden: false },
        { id: "q2", number: 5, stem: "Second", overridden: false },
      ],
    }],
  });

  assert.equal(container.children.length, 1);
  assert.equal(
    documentRef.created.some((node) => node.textContent.includes("<img src=x")),
    true,
  );
  assert.equal(documentRef.created.some((node) => "innerHTML" in node), false);
  assert.equal(
    documentRef.created.filter((node) => node.dataset.questionId).length,
    2,
  );
});

test("resolved review exposes the private preview link", () => {
  const documentRef = fakeDocument();
  const container = {
    children: [],
    replaceChildren(...children) { this.children = children; },
    append(child) { this.children.push(child); },
  };

  images.renderReview(documentRef, container, {
    run_id: "run-1",
    resolved: true,
    preview_url: "/studio/runs/run-1/preview",
    requirements: [],
  });

  const link = documentRef.created.find((node) => node.tagName === "a");
  assert.equal(link.textContent, "Preview quiz");
  assert.equal(link.href, "/studio/runs/run-1/preview");
});
