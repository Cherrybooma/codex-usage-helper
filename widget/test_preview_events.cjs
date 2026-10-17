// A small DOM/event simulation. Reads the actual preview listeners; no browser,
// Notion account, session logs, network requests or third-party dependency.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'preview/demo.html'), 'utf8');
const callbacks = new Map();
let closeCount = 0, selectedPage = 'overview', locked = true;
class MiniElement {
  constructor(id='', classes=[]) { this.id=id; this.classes=classes; this.parent=null; this.children=[]; }
  matches(selector) { return selector.startsWith('.') ? this.classes.includes(selector.slice(1)) : this.id===selector.slice(1); }
  closest(selector) { for(let node=this;node;node=node.parent) if(node.matches?.(selector)) return node; return null; }
  append(child) { child.parent=this;this.children.push(child);return child; }
  replaceChildren(...children) { for(const child of this.children)child.parent=null;this.children=[];for(const child of children)this.append(child); }
}
const mockDocument = {addEventListener(type, callback) {callbacks.set(type,callback);}};
const start = source.indexOf("document.addEventListener('click',");
const end = source.indexOf(';window.addEventListener',start);
assert(start>=0 && end>start, 'actual document click/key handlers found');
vm.runInNewContext(source.slice(start,end), {document:mockDocument, Element:MiniElement, close(){closeCount++;locked=false;}});
const doc=new MiniElement('document'),popover=doc.append(new MiniElement('popover')),card=popover.append(new MiniElement('card'));
function click(target, targetListener) {
  // DOM dispatch constructs the propagation path before target listeners run.
  const eventPath=[];for(let node=target;node;node=node.parent)eventPath.push(node);
  const event={target, composedPath:()=>eventPath};
  targetListener?.(event);
  callbacks.get('click')(event);
}
let passed=0;
const check=(condition,message)=>{assert(condition,message);passed++;};
const nav=card.append(new MiniElement('tabs')),tab=nav.append(new MiniElement('actions'));
click(tab,()=>{selectedPage='actions';card.replaceChildren(new MiniElement('new-card-content'));});
check(!tab.closest('#popover'), 'tab is detached during target handler, reproducing old closest() failure');
check(closeCount===0 && locked && selectedPage==='actions', 'page replacement survives document bubbling');
const cycle=card.append(new MiniElement('metric-refresh'));
click(cycle,()=>card.replaceChildren(new MiniElement('metrics-after-refresh')));
check(closeCount===0 && locked, 'metric refresh replacement also stays open');
const widget=doc.append(new MiniElement('widget'));
const orb=widget.append(new MiniElement('', ['orb']));
click(orb,()=>{selectedPage='overview';locked=true;doc.replaceChildren(popover,widget);widget.replaceChildren(new MiniElement('replacement-orb',['orb']));});
check(closeCount===0 && locked && selectedPage==='overview', 'another icon switches card without outside dismissal');
const pinnedOrb=widget.append(new MiniElement('', ['orb']));
click(pinnedOrb,()=>{closeCount++;locked=false;});
check(closeCount===1 && !locked, 'clicking same pinned icon can still close it');
locked=true;click(doc.append(new MiniElement('outside')));
check(closeCount===2 && !locked, 'outside click still closes');
locked=true;callbacks.get('keydown')({key:'Escape'});
check(closeCount===3 && !locked, 'Escape still closes');
const output={passed, failed:0, actual_preview_listener:true, simulated_detached_target:true};
console.log(JSON.stringify(output));
