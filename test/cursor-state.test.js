import test from 'node:test'
import assert from 'node:assert/strict'
import { installCursorThinking, actionForTool } from '../cursor-state.js'
function harness(request) {
  let listener
  const calls=[]
  const bridge={ready:true,async request(path,owner,options){calls.push({path,owner,...options.body});await request?.(options.body.active)}}
  const feedback=installCursorThinking({on(_name,fn){listener=fn}},bridge)
  return {calls,feedback,emit(id,type,data){listener({id},{type,data})}}
}
test('unrelated chat and health checks never summon commander',async()=>{
 const h=harness();h.emit('one','step/start');h.emit('one','turn/start');h.feedback.begin({id:'one'},'doraemon_health');await h.feedback.flush();assert.equal(h.calls.length,0);await h.feedback.dispose()
})
test('Doraemon work summons once, model wait renews, turn end cleans up',async()=>{
 const h=harness();h.feedback.begin({id:'one'},'doraemon_click',{button:'right'});h.emit('one','step/start');h.emit('one','turn/end');await h.feedback.flush();assert.deepEqual(h.calls.map(c=>c.active),[true,true,false]);assert.equal(h.calls[0].action,'right_click');assert.ok(h.calls[0].client_pid);await h.feedback.dispose()
})
test('a failed bridge request cannot prevent final cleanup',async()=>{
 const h=harness(active=>{if(active)throw Error('offline')});h.feedback.begin({id:'one'},'doraemon_run');await h.feedback.flush();h.feedback.end({id:'one'});await h.feedback.flush();assert.deepEqual(h.calls.map(c=>c.active),[true,false]);await h.feedback.dispose()
})
test('finishing one task does not hide another owner; dispose hides remaining',async()=>{
 const h=harness();h.feedback.begin({id:'one'},'doraemon_run');h.feedback.begin({id:'two'},'doraemon_type');await h.feedback.flush();h.emit('one','turn/end');await h.feedback.flush();await h.feedback.dispose();assert.notEqual(h.calls[0].owner,h.calls[1].owner);assert.equal(h.calls.at(-1).owner,h.calls[1].owner);assert.equal(h.calls.at(-1).active,false)
})
test('spell poses cover typing, scrolling and dragging',()=>{
 assert.equal(actionForTool('doraemon_type'),'type');assert.equal(actionForTool('doraemon_scroll'),'scroll');assert.equal(actionForTool('doraemon_swipe'),'drag')
})
