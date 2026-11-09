const $ = id => document.getElementById(id);
let batch = null;
let previousQuestions = [];

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw Error(body.detail || 'Request failed.');
  }
  return response;
}

async function run(button, message, action) {
  button.disabled = true;
  $('status').textContent = message;
  try {
    await action();
    $('status').textContent = '';
  } catch (error) {
    $('status').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function addText(parent, tag, text) {
  const element = document.createElement(tag);
  element.textContent = text;
  parent.append(element);
  return element;
}

function showResult(parent, result) {
  if (result.answer_source) {
    const source = result.answer_source === 'groq'
      ? 'Groq answer checked against local evidence.'
      : 'Answer calculated locally.';
    addText(parent, 'p', source);
  }
  if (result.provider_status) addText(parent, 'p', result.provider_status);
  addText(parent, 'p', result.message);

  for (const [title, items] of [
    [result.mode === 'chat' ? 'Evidence' : 'Verified totals', result.facts],
    ['Groq highlights', result.highlights],
    ['Budgeting tips', result.tips],
  ]) {
    if (!items?.length) continue;
    addText(parent, 'h3', title);
    const list = parent.appendChild(document.createElement('ul'));
    for (const text of items) {
      addText(list, 'li', text);
    }
  }
}

$('upload').addEventListener('submit', event => {
  event.preventDefault();
  run(event.submitter, 'Scrubbing and saving locally…', async () => {
    const form = new FormData();
    for (const file of $('files').files) form.append('files', file);
    const uploaded = await (await request('/api/batches', { method: 'POST', body: form })).json();
    const path = `/api/batches/${uploaded.id}`;
    const [text, storage] = await Promise.all([
      request(path).then(response => response.text()),
      request(`${path}/storage`).then(response => response.json()),
    ]);

    batch = uploaded.id;
    previousQuestions = [];
    $('scrubbed').textContent = text;
    $('download').href = path;
    $('stored').textContent = `${storage.rows.length} files saved in PostgreSQL ${storage.postgresql_version}.`;
    $('analysis').replaceChildren();
    $('messages').replaceChildren();
    $('preview').hidden = $('insights').hidden = false;
    $('chat').hidden = true;
  });
});

$('analyze').addEventListener('click', event => {
  const currentBatch = batch;
  run(event.currentTarget, 'Generating analysis…', async () => {
    const result = await (await request(`/api/batches/${currentBatch}/analysis`, { method: 'POST' })).json();
    if (batch !== currentBatch) return;
    $('analysis').replaceChildren();
    addText($('analysis'), 'p', `${result.model} · ${result.parsed_rows} parsed rows · ${result.status}`);
    showResult($('analysis'), result);
    $('chat').hidden = false;
  });
});

$('ask').addEventListener('submit', event => {
  event.preventDefault();
  const question = $('question').value.trim();
  if (!question || !batch) return;
  const currentBatch = batch;

  run(event.submitter, 'Checking the question against your statement…', async () => {
    const result = await (await request(`/api/batches/${currentBatch}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, previous_questions: previousQuestions }),
    })).json();
    if (batch !== currentBatch) return;

    const entry = $('messages').appendChild(document.createElement('article'));
    addText(entry, 'strong', question);
    showResult(entry, result);
    previousQuestions = [...previousQuestions, question].slice(-3);
    $('question').value = '';
  });
});
