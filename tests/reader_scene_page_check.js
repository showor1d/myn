// Exercise the real page functions with a minimal DOM; no browser or user data needed.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const script = fs.readFileSync(require('path').join(__dirname, '../src/web/app.js'), 'utf8');
const functions = ['showReader', 'openReader', 'reportReader', 'closeReader'].map(name => {
  const match = script.match(new RegExp('function ' + name + '\\([^]*?\\n}'));
  assert(match, name);
  return match[0];
}).join('\n');
const branch = script.split('case "reader":')[1].split('case "selection_used":')[0];
const context = vm.createContext({assert});
vm.runInContext(`
let readerKey=null, readerRevision=0, readerText='', readerReportedText='';
const dismissed=new Set(), events=[];
const reader={hidden:true};
const readerScroll={scrollTop:0, clientHeight:100, scrollHeight:100};
const readerBody={textContent:'', appendChild(){}, lastElementChild:null, querySelectorAll(){return []}};
const document={body:{classList:{add(){},remove(){}}}};
function renderMarkdown(x){return x}
function sizeInk(){} function clearLasso(){} function setTool(){} function layoutText(){}
function emit(event){events.push(event)}
${functions}
function receive(cmd){switch(cmd.cmd){case 'reader':${branch}}}
showReader('old','first streaming fragment',true);
const oldRevision=readerRevision;
showReader('old','complete lesson',true);
showReader('old','complete lesson',false);
assert.equal(events[events.length-1].text,'complete lesson');
assert.equal(readerRevision,oldRevision);
showReader('new','new content',false);
receive({cmd:'reader',action:'close',key:'old',revision:oldRevision});
assert.equal(reader.hidden,false,'late result must not close new reply');
let revision=readerRevision;
receive({cmd:'reader',action:'close',key:'new',revision});
assert.equal(reader.hidden,true,'matching scene closes');
openReader();
receive({cmd:'reader',action:'close',key:'new',revision});
assert.equal(reader.hidden,false,'reopening invalidates old decisions');
receive({cmd:'reader',action:'close'});
assert.equal(reader.hidden,true,'manual close still works');
`, context);
console.log('Reader page lifecycle and stale-result guards: PASS');
