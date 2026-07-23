# Upstream attribution

Canvas session pagination and managed-download patterns were informed by
[`jasp-nerd/canvas-course-downloader`](https://github.com/jasp-nerd/canvas-course-downloader),
release `v2.10.0`, licensed under the MIT License included in this directory.

The rendered Panopto transcript extraction approach in `lib/panopto-page.js`
was informed by
[`minjunminji/panopto-lecture-transcript-scraper`](https://github.com/minjunminji/panopto-lecture-transcript-scraper),
which is available under the MIT License.

This companion is a purpose-built derivative. It intentionally excludes course
exports, grades, quizzes, assignments, submissions, announcements, discussions,
and arbitrary Canvas file crawling.
