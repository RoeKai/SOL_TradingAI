'use strict';
document.querySelector('#login').addEventListener('submit', async event => {
  event.preventDefault();
  const message = document.querySelector('#message');
  const button = document.querySelector('button');
  button.disabled = true;
  try {
    const response = await fetch(`${document.body.dataset.base}/login`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, credentials: 'same-origin',
      body: JSON.stringify({token: document.querySelector('#token').value})
    });
    if (!response.ok) throw new Error(response.status === 429 ? '请求过于频繁，请稍后重试。' : '登录失败，请检查令牌。');
    document.querySelector('#token').value = '';
    window.location.assign(`${document.body.dataset.base}/`);
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
});
