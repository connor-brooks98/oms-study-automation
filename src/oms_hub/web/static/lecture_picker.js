(function () {
  'use strict';
  const PAGE_SIZE = 20;
  const catalogs = new WeakMap();

  function filterRows(rows, course, exam, query) {
    const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    return rows.filter(row => (!course || row.course === course) && (!exam || String(row.exam) === exam) &&
      words.every(word => `${row.label} ${row.course} ${row.exam}`.toLocaleLowerCase().includes(word)));
  }

  function initialize(root) {
    if (root.lecturePicker) return root.lecturePicker;
    const doc = root.ownerDocument;
    const field = name => root.querySelector(`[data-picker-${name}]`);
    const search = field('search'), course = field('course'), exam = field('exam');
    const initial = JSON.parse(root.dataset.selected || '[]').map(String);
    const selected = new Set(initial), multiple = root.dataset.multiple === 'true';
    let required = root.dataset.required === 'true', page = 0;
    let rows;
    try {
      const catalog = doc.getElementById(root.dataset.catalogId);
      if (!catalog) throw Error('Missing lecture catalog');
      if (!catalogs.has(catalog)) catalogs.set(catalog, JSON.parse(catalog.textContent));
      rows = catalogs.get(catalog);
      if (!Array.isArray(rows)) throw Error('Invalid lecture catalog');
    } catch (_) {
      field('status').textContent = 'The lecture list could not load. Reload this page to try again.';
      search.setCustomValidity('Reload this page to load lectures.');
      return null;
    }
    const byId = new Map(rows.map(row => [String(row.id), row]));
    function option(value, label) {
      const node = doc.createElement('option'); node.value = value; node.textContent = label; return node;
    }
    course.replaceChildren(option('', 'All courses'), ...[...new Set(rows.map(row => row.course))].sort().map(value => option(value, value)));
    function updateExams() {
      const previous = exam.value;
      const exams = [...new Set(rows.filter(row => !course.value || row.course === course.value).map(row => String(row.exam)))].sort((a, b) => Number(a) - Number(b));
      exam.replaceChildren(option('', 'All exams'), ...exams.map(value => option(value, `Exam ${value}`)));
      exam.value = exams.includes(previous) ? previous : '';
    }
    function validity() { search.setCustomValidity(required && !selected.size ? 'Select at least one lecture or source from the results.' : ''); }
    function changed() { root.dispatchEvent(new Event('change', { bubbles: true })); }
    function choose(id, checked) {
      if (!multiple) selected.clear();
      if (checked) selected.add(id); else selected.delete(id);
      render(); changed();
    }
    function render() {
      validity();
      field('values').replaceChildren(...[...selected].map(id => {
        const node = doc.createElement('input'); node.type = 'hidden'; node.name = root.dataset.name; node.value = id; return node;
      }));
      const summary = field('selection');
      if (!selected.size) {
        const text = doc.createElement('span'); text.textContent = 'No selection yet.'; summary.replaceChildren(text);
      } else summary.replaceChildren(...[...selected].map(id => {
        const button = doc.createElement('button'); button.type = 'button'; button.className = 'sh-btn sh-btn--secondary';
        button.textContent = `${byId.get(id)?.label || `Unavailable source ${id}`} · Remove`;
        button.addEventListener('click', () => { choose(id, false); search.focus(); }); return button;
      }));
      const matches = filterRows(rows, course.value, exam.value, search.value);
      page = Math.min(page, Math.max(0, Math.ceil(matches.length / PAGE_SIZE) - 1));
      const visible = matches.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
      field('status').textContent = !rows.length ? 'No lectures or sources are available yet.' : !matches.length ? 'No matching lectures. Try another course, exam or search.' :
        `${selected.size} selected · Showing ${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + visible.length} of ${matches.length}`;
      const focusId = String(visible.find(row => selected.has(String(row.id)))?.id ?? visible[0]?.id ?? '');
      field('results').replaceChildren(...visible.map(row => {
        const id = String(row.id), label = doc.createElement('label'), input = doc.createElement('input'), text = doc.createElement('span');
        label.className = 'lecture-picker__option'; input.type = multiple ? 'checkbox' : 'radio';
        input.value = id; input.checked = selected.has(id);
        if (!multiple) input.tabIndex = id === focusId ? 0 : -1;
        input.dataset.pickerChoice = id; text.textContent = row.label;
        input.addEventListener('change', event => {
          event.stopPropagation(); choose(id, input.checked);
          Array.from(field('results').querySelectorAll('input')).find(node => node.value === id)?.focus();
        });
        if (!multiple) input.addEventListener('keydown', event => {
          const direction = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 :
            ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 0;
          if (!direction) return;
          event.preventDefault();
          const index = visible.findIndex(item => String(item.id) === id);
          const next = String(visible[(index + direction + visible.length) % visible.length].id);
          choose(next, true);
          Array.from(field('results').querySelectorAll('input')).find(node => node.value === next)?.focus();
        });
        label.append(input, text); return label;
      }));
      field('previous').disabled = page === 0; field('next').disabled = (page + 1) * PAGE_SIZE >= matches.length;
    }
    for (const node of [course, exam]) node.addEventListener('change', event => {
      event.stopPropagation(); if (node === course) updateExams(); page = 0; render();
    });
    search.addEventListener('input', () => { page = 0; render(); });
    search.addEventListener('change', event => event.stopPropagation());
    field('previous').addEventListener('click', () => { page--; render(); });
    field('next').addEventListener('click', () => { page++; render(); });
    root.closest('form')?.addEventListener('reset', () => queueMicrotask(() => {
      selected.clear(); initial.forEach(id => selected.add(id)); updateExams(); page = 0; render(); changed();
    }));
    updateExams(); render();
    root.lecturePicker = { getValues: () => [...selected], setDisabled(value) { root.disabled = value; }, setRequired(value) { required = value; validity(); } };
    return root.lecturePicker;
  }
  function initializeAll(doc) { doc.querySelectorAll('[data-lecture-picker]').forEach(initialize); }
  const api = { initialize, initializeAll, filterRows, PAGE_SIZE };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.StudyHubLecturePicker = api;
  if (typeof document !== 'undefined') initializeAll(document);
}());
