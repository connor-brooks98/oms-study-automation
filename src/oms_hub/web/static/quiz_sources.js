(function () {
  'use strict';
  function mount(container, context) {
    if (!container || !context || !context.sessionId || !context.attemptId) return;
    const details = document.createElement('details');
    details.className = 'quiz-sources';
    const summary = document.createElement('summary');
    summary.textContent = 'Lecture sources';
    const body = document.createElement('div');
    body.className = 'quiz-sources-body';
    body.setAttribute('aria-live', 'polite');
    details.append(summary, body);
    container.append(details);
    let loaded = false;
    details.addEventListener('toggle', async function () {
      if (!details.open || loaded) return;
      loaded = true;
      body.textContent = 'Loading cited sources…';
      const base = '/study/sessions/' + encodeURIComponent(context.sessionId) + '/sources/' + encodeURIComponent(context.attemptId);
      try {
        const response = await fetch(base, {credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok) throw new Error('unavailable');
        const data = await response.json();
        body.replaceChildren();
        if (!data.available || !Array.isArray(data.sources) || !data.sources.length) {
          body.textContent = data.message || 'Source preview unavailable for this quiz.';
          return;
        }
        data.sources.forEach(function (source) {
          const article = document.createElement('article');
          const heading = document.createElement('h4');
          heading.textContent = source.title + ' · ' + (source.location || source.locator);
          const excerpt = document.createElement('blockquote');
          excerpt.textContent = source.excerpt + (source.truncated ? ' …' : '');
          article.append(heading, excerpt);
          // Accept only this authenticated attempt's server-generated indexed route.
          if (typeof source.original_url === 'string' && source.original_url.startsWith(base + '/') && /^\d+\/original(?:#page=\d+)?$/.test(source.original_url.slice(base.length + 1))) {
            const link = document.createElement('a');
            link.href = source.original_url;
            link.textContent = source.link_label || 'Open original source';
            link.target = '_blank';
            link.rel = 'noopener';
            article.append(link);
          }
          body.append(article);
        });
      } catch (_) {
        body.textContent = 'Source preview unavailable. Your saved answer is unchanged.';
      }
    });
    return details;
  }
  window.StudyHubQuizSources = {mount: mount};
})();
