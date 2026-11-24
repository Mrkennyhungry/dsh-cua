import { createHash } from 'node:crypto'
import { createServer } from 'node:http'

export async function startFixtures() {
  const trials = new Map()
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1')
    const [, , id, page = 'form'] = url.pathname.split('/')
    const trial = trials.get(id)
    if (!trial) {
      response.writeHead(404).end()
      return
    }
    if (page === 'gate') {
      response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ unlocked: trial.unlocked === true }))
      return
    }
    if (request.method === 'POST' && page === 'event') {
      let bytes = 0
      const chunks = []
      for await (const chunk of request) {
        bytes += chunk.length
        if (bytes > 16384) {
          response.writeHead(413).end()
          return
        }
        chunks.push(chunk)
      }
      try {
        trial.events.push({ at: Date.now(), ...JSON.parse(Buffer.concat(chunks)),
          native_watcher_started_at: trial.watchStartedAt ?? null })
        response.writeHead(200, { 'content-type': 'application/json' }).end('{}')
      } catch {
        response.writeHead(400).end()
      }
      return
    }
    const report = `async function report(value) { await fetch('/run/${id}/event', {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(value)}) }`
    const pages = {
      form: `<form onsubmit="submitForm(event)">
        <label>备注<input name="note"></label><label>测试口令<input type="password" name="password"></label>
        <label>加急<input type="checkbox" name="urgent"></label>
        <label>城市<select name="city"><option>杭州</option><option>上海</option></select></label>
        <button>保存表单</button></form><output>待保存</output>
        <script>${report}
        async function submitForm(event) { event.preventDefault(); const f=event.target;
          const value={kind:'form',note:f.note.value,password:f.password.value,urgent:f.urgent.checked,city:f.city.value};
          await report(value);document.querySelector('output').textContent='已保存 '+JSON.stringify(value); }
        </script>`,
      upload: `<label>附件<input type="file" onchange="uploadFile(this)"></label><output>待上传</output>
        <script>${report}
        async function uploadFile(input) { const file=input.files[0]; if(!file)return;
          const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer())),b=>b.toString(16).padStart(2,'0')).join('');
          await report({kind:'upload',name:file.name,sha256:digest});document.querySelector('output').textContent=file.name+' SHA256 '+digest; }
        </script>`,
      tabs: `<h1>同 URL 标签</h1><p>本标签标识 <strong id="tab"></strong></p>
        <button onclick="window.open(location.href,'P7_SECOND_${id}')">打开第二个相同 URL 标签</button>
        <button onclick="report({kind:'frame',tab:window.name,level:'main'})">重复按钮</button>
        <iframe title="outer" src="/run/${id}/outer"></iframe>
        <script>${report}document.querySelector('#tab').textContent=window.name||'FIRST';</script>`,
      outer: `<button onclick="report({kind:'frame',tab:top.name,level:'outer'})">重复按钮</button>
        <iframe title="inner" src="/run/${id}/inner"></iframe><script>${report}</script>`,
      inner: `<button onclick="this.textContent='inner done';report({kind:'frame',tab:top.name,level:'inner'})">重复按钮</button><script>${report}</script>`,
      toast: `<button onclick="showToast()">显示短提示</button><output>待触发</output>
        <script>${report}function showToast() {
          const watcher=Object.keys(window).filter(key=>key.startsWith('__dsh_watch_')).map(key=>window[key]).find(state=>state?.armed);
          report({kind:'toast',text:'P7 transient saved',watcher_armed:!!watcher,watcher_dispatched_at:watcher?.dispatched_at||null});
          const el=document.createElement('div');el.setAttribute('role','status');el.textContent='P7 transient saved';
          document.body.append(el);setTimeout(()=>el.remove(),120); }</script>`,
      persistence: `<h1>持久化 fixture</h1><button onclick="setLogin()">设置 fixture 登录态</button>
        <button onclick="document.cookie='P7_LOGIN=;path=/run/${id};max-age=0';document.querySelector('#state').textContent='登录已失效，请用户重新登录';report({kind:'expired'})">模拟登录失效</button>
        <button onclick="document.querySelector('#state').textContent='请用户完成验证码后在当前目标继续';report({kind:'captcha'})">模拟验证码</button>
        <output id="state"></output><script>${report}
          function display() { document.querySelector('#state').textContent=document.cookie+' '+localStorage.getItem('P7_SITE'); }
          async function setLogin() { document.cookie='P7_LOGIN=${id};path=/run/${id};max-age=3600';localStorage.setItem('P7_SITE','${id}');await report({kind:'login',value:'${id}'});display(); }
          display();report({kind:'restore',cookie:document.cookie,site:localStorage.getItem('P7_SITE')});</script>`,
      cancellation: `<label>会话归属<input id="owner" oninput="report({kind:'input',value:this.value})"></label>
        <button onclick="report({kind:'owner',value:document.querySelector('input').value});this.textContent='已保存'">保存归属</button>
        <p>这里永远不会出现等待的结束标记</p><script>${report}</script>`,
      cancellation_b: `<label>会话归属<input id="owner" oninput="report({kind:'input',value:this.value})"></label>
        <button disabled onclick="report({kind:'owner',value:document.querySelector('input').value});this.textContent='已保存'">保存归属</button>
        <p id="gate">等待取消方停止</p><script>${report}
        const gate=setInterval(async()=>{const state=await (await fetch('/run/${id}/gate')).json();if(state.unlocked){clearInterval(gate);document.querySelector('button').disabled=false;document.querySelector('#gate').textContent='取消方已停止';}},100);</script>`,
    }
    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
    response.end(`<!doctype html><meta charset="utf-8"><title>P7 ${id}</title>
      <style>body{font:20px sans-serif;margin:32px}label{display:block;margin:15px 0}button,input,select{font:inherit}iframe{display:block;width:90%;height:240px}</style>
      ${pages[page] || '<p>Unknown fixture</p>'}`)
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  return {
    origin: `http://127.0.0.1:${server.address().port}`,
    add(id, file) {
      const trial = { events: [], file, sha256: createHash('sha256').update(file).digest('hex') }
      trials.set(id, trial)
      return trial
    },
    close: () => new Promise(resolve => {
      server.close(resolve)
      server.closeAllConnections()
    }),
  }
}

export function verifyFixture(task, trial, id, fileName) {
  const events = trial.events
  if (task === 'form') {
    const rows = events.filter(event => event.kind === 'form')
    return rows.length === 1 && rows[0].note === `P7-${id}` && rows[0].password === 'fixture-only-42'
      && rows[0].urgent === true && rows[0].city === '上海'
  }
  if (task === 'upload') {
    const rows = events.filter(event => event.kind === 'upload')
    return rows.length === 1 && rows[0].name === fileName && rows[0].sha256 === trial.sha256
  }
  if (task === 'frames') {
    const rows = events.filter(event => event.kind === 'frame')
    return rows.length === 1 && rows[0].tab === `P7_SECOND_${id}` && rows[0].level === 'inner'
  }
  if (task === 'toast') {
    const rows = events.filter(event => event.kind === 'toast')
    return rows.length === 1 && (rows[0].watcher_armed === true
      && Date.parse(rows[0].watcher_dispatched_at) <= rows[0].at
      || typeof rows[0].native_watcher_started_at === 'number'
      && rows[0].native_watcher_started_at <= rows[0].at)
  }
  if (task === 'cancellation') {
    const rows = events.filter(event => event.kind === 'owner')
    return rows.length === 1 && rows[0].value === `A-${id}`
  }
  return false
}
