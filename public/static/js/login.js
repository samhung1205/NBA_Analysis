/** 登入 / 註冊 (規格書 §3.2) */

(function () {
  const form = document.getElementById('auth-form');
  const msg = document.getElementById('auth-msg');
  const modeBtns = document.querySelectorAll('[data-mode]');
  const nameField = document.getElementById('name-field');
  const submitBtn = document.getElementById('auth-submit');
  let mode = 'login';

  const next = new URLSearchParams(location.search).get('next') || '/';

  modeBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      mode = btn.dataset.mode;
      modeBtns.forEach((b) => {
        const active = b.dataset.mode === mode;
        b.classList.toggle('bg-slate-800', active);
        b.classList.toggle('text-slate-100', active);
        b.classList.toggle('text-slate-500', !active);
      });
      nameField.classList.toggle('hidden', mode !== 'register');
      submitBtn.textContent = mode === 'login' ? '登入' : '註冊並登入';
      msg.textContent = '';
    });
  });

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    msg.className = 'text-xs text-slate-400';
    msg.textContent = '處理中…';
    try {
      const body = { email: fd.get('email'), password: fd.get('password') };
      if (mode === 'register') body.display_name = fd.get('display_name') || undefined;
      await NBA.post(mode === 'login' ? '/api/auth/login' : '/api/auth/register', body);
      msg.className = 'text-xs text-green-400';
      msg.textContent = '成功，正在跳轉…';
      location.href = next;
    } catch (e) {
      msg.className = 'text-xs text-red-400';
      msg.textContent = e.message;
    }
  });

  NBA.currentUser().then((u) => {
    if (u) {
      msg.className = 'text-xs text-slate-400';
      msg.innerHTML = `已以 ${NBA.esc(u.email)} 登入。<a href="${NBA.esc(next)}" class="text-orange-400 underline">繼續</a>`;
    }
  });
})();
