(() => {
  const editor = document.querySelector('[data-post-editor]');
  if (!editor) return;
  const form = editor.closest('form');
  const output = form.querySelector('[name=body_html]');
  const count = document.querySelector('[data-post-count]');
  const error = document.querySelector('[data-post-error]');
  const escape = s => s.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
  const validURL = s => { try { const u = new URL(s); return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password; } catch { return false; } };
  const serialize = node => {
    if (node.nodeType === Node.TEXT_NODE) return escape(node.textContent);
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const tag = node.tagName.toLowerCase();
    if (tag === 'br') return '\n';
    const children = () => [...node.childNodes].map(serialize).join('');
    const blockTags = ['div','p','blockquote','pre'];
    const block = html => {
      const previous = node.previousSibling, next = node.nextSibling;
      const before = previous && !previous.textContent.endsWith('\n') ? '\n' : '';
      const after = next && !(next.nodeType === Node.ELEMENT_NODE && blockTags.includes(next.tagName.toLowerCase())) && !next.textContent.startsWith('\n') ? '\n' : '';
      return before + html + after;
    };
    if (['div','p'].includes(tag)) return block(children());
    if (tag === 'pre') return block(`<pre>${escape(node.textContent)}</pre>`);
    if (tag === 'code') return `<code>${escape(node.textContent)}</code>`;
    if (tag === 'blockquote') return block(`<blockquote>${children()}</blockquote>`);
    const mapped = {strong:'b', em:'i', strike:'s', del:'s', 'tg-spoiler':'tg-spoiler'}[tag] || tag;
    if (['b','i','u','s','blockquote','tg-spoiler'].includes(mapped)) return `<${mapped}>${children()}</${mapped}>`;
    if (tag === 'a' && validURL(node.getAttribute('href') || '')) return `<a href="${escape(node.getAttribute('href'))}">${children()}</a>`;
    return children();
  };
  const value = () => [...editor.childNodes].map(serialize).join('');
  const update = () => { count.textContent = `${editor.innerText.length} / 3000`; output.value = value(); };
  let savedRange;
  const saveRange = () => {
    const selection = window.getSelection();
    if (selection.rangeCount && editor.contains(selection.anchorNode) && editor.contains(selection.focusNode)) savedRange = selection.getRangeAt(0).cloneRange();
  };
  editor.addEventListener('keyup', saveRange); editor.addEventListener('mouseup', saveRange);
  const restore = () => {
    editor.focus();
    if (savedRange) { const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(savedRange); }
  };
  const wrap = tag => {
    const selection = window.getSelection();
    if (!selection.rangeCount || selection.isCollapsed) { error.textContent = 'Сначала выделите текст.'; return; }
    const range = selection.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return;
    const wrapper = document.createElement(tag);
    wrapper.append(range.extractContents()); range.insertNode(wrapper);
    range.selectNodeContents(wrapper); selection.removeAllRanges(); selection.addRange(range);
  };
  const commands = {bold:'bold', italic:'italic', underline:'underline', strike:'strikeThrough', clear:'removeFormat'};
  const apply = action => {
    restore(); error.textContent = '';
    if (commands[action]) document.execCommand(commands[action], false);
    else if (action === 'quote') document.execCommand('formatBlock', false, 'blockquote');
    else if (action === 'code') wrap('code');
    else if (action === 'spoiler') wrap('tg-spoiler');
    else if (action === 'link') {
      const url = window.prompt('Ссылка (https://…)');
      if (url && validURL(url)) document.execCommand('createLink', false, url);
      else if (url) error.textContent = 'Нужна корректная ссылка http:// или https://.';
    } else if (action === 'date') document.execCommand('insertText', false, new Date().toLocaleDateString('ru-RU'));
    saveRange(); update();
  };
  document.querySelectorAll('[data-format]').forEach(button => {
    button.addEventListener('mousedown', e => e.preventDefault());
    button.addEventListener('click', () => apply(button.dataset.format));
  });
  editor.addEventListener('keydown', e => {
    if (!(e.ctrlKey || e.metaKey)) return;
    const key = e.key.toLowerCase();
    const action = e.shiftKey ? {x:'strike',p:'spoiler',m:'code',d:'date'}[key] : {b:'bold',i:'italic',u:'underline',k:'link'}[key];
    if (action) { e.preventDefault(); saveRange(); apply(action); }
  });
  editor.addEventListener('paste', e => {
    // Plain text paste cannot import scripts, styles or unsupported Telegram entities.
    e.preventDefault(); document.execCommand('insertText', false, e.clipboardData.getData('text/plain')); update();
  });
  editor.addEventListener('input', update);
  form.addEventListener('submit', e => {
    update();
    for (const code of editor.querySelectorAll('code,pre')) {
      if (code.parentElement.closest('b,strong,i,em,u,s,strike,tg-spoiler,a,blockquote,code,pre')) {
        e.preventDefault(); error.textContent = 'Моноширинный текст нельзя совмещать с другим форматированием. Снимите лишнее оформление.'; return;
      }
    }
    if (!editor.innerText.trim() || editor.innerText.length > 3000) {
      e.preventDefault(); error.textContent = 'Пост: от 1 до 3000 символов.'; editor.focus();
    }
  });
  update();
})();
