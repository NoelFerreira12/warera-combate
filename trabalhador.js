// O assistente de combate no browser: o Python do projecto (Pyodide) a correr neste Web Worker.
// A pagina fala com ele por mensagens, com os mesmos caminhos da API local (/api/estado, ...).
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.26.4/full/';
importScripts(PYODIDE + 'pyodide.js');

const RAIZ = '/home/pyodide/painel';
let py = null, nav = null;
const fila = [];                       // pedidos da pagina que chegam enquanto o Python le o jogo
const visto = {versao: -1, recursos: -1, drops: -1};

const post = m => self.postMessage(m);
const dormir = ms => new Promise(r => setTimeout(r, ms));
const sincronizar = popular => new Promise((ok, falha) => py.FS.syncfs(popular, e => e ? falha(e) : ok()));

function escrever(caminho, texto) {
  const dir = caminho.split('/').slice(0, -1).join('/');
  py.FS.mkdirTree(dir);
  py.FS.writeFile(caminho, texto);
}

async function iniciar(cfg) {
  post({tipo: 'progresso', txt: 'python'});
  py = await loadPyodide({indexURL: PYODIDE});
  post({tipo: 'progresso', txt: 'numpy'});
  await py.loadPackage(['numpy', 'sqlite3']);
  const man = await (await fetch('manifesto.json', {cache: 'no-cache'})).json();
  post({tipo: 'progresso', txt: 'motor'});
  // de 4 em 4, com repeticoes: um servidor estatico pequeno recusa muitas ligacoes ao mesmo tempo
  const buscar = async url => {
    for (let i = 0; ; i++) {
      try { const r = await fetch(url); if (!r.ok) throw new Error(r.status + ' ' + url); return await r.text(); }
      catch (e) { if (i >= 3) throw e; await dormir(500 * (i + 1)); }
    }
  };
  for (let i = 0; i < man.ficheiros.length; i += 4) {
    const lote = man.ficheiros.slice(i, i + 4);
    const textos = await Promise.all(lote.map(f => buscar('py/' + f + '?v=' + man.versao)));
    lote.forEach((f, j) => escrever(RAIZ + '/' + f, textos[j]));
  }
  // o que o assistente guarda (historico, rankings, validacao): no IndexedDB deste browser
  py.FS.mkdirTree(RAIZ + '/saida');
  py.FS.mount(py.FS.filesystems.IDBFS, {}, RAIZ + '/saida');
  await sincronizar(true);
  for (const s of man.sementes) {
    const destino = RAIZ + '/' + s.destino;
    if (!py.FS.analyzePath(destino).exists)
      escrever(destino, await buscar('semente/' + s.ficheiro + '?v=' + man.versao));
  }
  py.runPython(`import sys\nif ${JSON.stringify(RAIZ)} not in sys.path: sys.path.insert(0, ${JSON.stringify(RAIZ)})`);
  nav = py.pyimport('warera.combate.navegador');
  const uid = nav.iniciar(cfg.jogador || null, true);   // sem jogador escolhido: pede o nome primeiro
  post({tipo: 'pronto', jogador: uid, versao: man.versao});
  setInterval(() => sincronizar(false).catch(() => {}), 60000);
  ciclo();
}

function corpo(metodo, caminho, dados) {
  return JSON.parse(nav.pedido(metodo, caminho, dados || ''));
}

function atender() {
  while (fila.length) {
    const m = fila.shift();
    let r;
    try { r = corpo(m.metodo, m.caminho, m.corpo); }
    catch (e) { r = {codigo: 500, corpo: JSON.stringify({erro: String(e).slice(0, 300)})}; }
    post({tipo: 'resposta', id: m.id, codigo: r.codigo, corpo: r.corpo});
    if (m.metodo === 'POST' && m.caminho === '/api/jogador') visto.versao = visto.recursos = visto.drops = -1;
  }
}

function publicar(v) {
  post({tipo: 'api', caminho: '/api/estado', corpo: corpo('GET', '/api/estado').corpo});
  if (v.versao !== visto.versao || v.recursos !== visto.recursos) {
    visto.versao = v.versao; visto.recursos = v.recursos;
    post({tipo: 'api', caminho: '/api/estrategia', corpo: corpo('GET', '/api/estrategia').corpo});
  }
  if (v.drops !== visto.drops) {
    visto.drops = v.drops;
    post({tipo: 'api', caminho: '/api/drops', corpo: corpo('GET', '/api/drops').corpo});
  }
}

async function ciclo() {
  for (;;) {
    atender();
    let v;
    try { v = JSON.parse(nav.passo()); }
    catch (e) { post({tipo: 'erro', txt: String(e).slice(0, 400)}); await dormir(2000); continue; }
    atender();
    try { publicar(v); } catch (e) { post({tipo: 'erro', txt: String(e).slice(0, 400)}); }
    await dormir(v.lidas.length ? 60 : 500);
  }
}

self.onmessage = e => {
  const m = e.data;
  if (m.tipo === 'iniciar') iniciar(m).catch(err => post({tipo: 'erro', txt: String(err).slice(0, 400)}));
  else if (m.tipo === 'pedido') { fila.push(m); if (!nav) return; }
  else if (m.tipo === 'guardar' && py) sincronizar(false).catch(() => {});
};
