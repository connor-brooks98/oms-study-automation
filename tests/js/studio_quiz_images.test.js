const test = require("node:test");
const assert = require("node:assert/strict");

const studioImages = require("../../src/oms_hub/web/static/studio_quiz_images.js");

// -- Minimal fake DOM sufficient to drive renderReview()/renderLoadError() --

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.className = "";
    this.dataset = {};
    this.textContent = "";
    this.type = "";
    this.href = "";
    this.children = [];
    this._listeners = {};
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  addEventListener(type, handler) {
    (this._listeners[type] ||= []).push(handler);
  }
}

class FakeContainer extends FakeElement {
  replaceChildren() {
    this.children = [];
  }
}

const fakeDocument = {
  createElement: (tag) => new FakeElement(tag),
};

test("renderLoadError clears the container, shows the message, and retries on click", () => {
  const container = new FakeContainer("div");
  container.children.push(new FakeElement("article"));
  let retried = false;

  studioImages.renderLoadError(
    fakeDocument,
    container,
    "Image review could not be loaded.",
    () => {
      retried = true;
    },
  );

  assert.equal(container.children.length, 1);
  const wrap = container.children[0];
  assert.equal(wrap.className, "studio-image-load-error");
  const [message, retryButton] = wrap.children;
  assert.equal(message.textContent, "Image review could not be loaded.");
  assert.equal(retryButton.textContent, "Retry");
  assert.equal(retryButton.dataset.retryImageReview, "true");
  assert.equal(retried, false);

  const [handler] = retryButton._listeners.click;
  handler();
  assert.equal(retried, true);
});

test("renderReview renders an upload form with a submit button per requirement", () => {
  const container = new FakeContainer("div");

  studioImages.renderReview(fakeDocument, container, {
    resolved: false,
    preview_url: null,
    requirements: [
      {
        image_key: "img-1",
        source_title: "Lecture slides",
        locator: "Slide 4",
        description: "Diagram of the nephron",
        uploaded: false,
        questions: [],
      },
    ],
  });

  assert.equal(container.children.length, 1);
  const card = container.children[0];
  const form = card.children.find((child) => child.tagName === "form");
  assert.ok(form, "expected an upload form on the requirement card");
  const submitButton = form.children.find(
    (child) => child.tagName === "button",
  );
  assert.equal(submitButton.type, "submit");
  assert.equal(submitButton.textContent, "Upload image");
});
