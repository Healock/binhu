// Real application-shell verification; every API response is synthetic.
import {createRequire} from 'node:module'
import {mkdirSync,writeFileSync} from 'node:fs'
import assert from 'node:assert/strict'
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright')
const out='../scratch/address-ux'; mkdirSync(out,{recursive:true})
const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'}), context=await browser.newContext({viewport:{width:1280,height:960}}), page=await context.newPage()
const errors=[]; page.on('pageerror',e=>errors.push(e.stack || e.message))
let failNext=0
let readonly=false
let optionFailure=false
let delayedId=0
const entries=Array.from({length:23},(_,i)=>({id:i+1,status:'unmatched',revision:1,selected:null}))
const task=e=>({task_key:`全链条:fixture-${e.id}`,parser_type:'全链条',row_key:`fixture-${e.id}`,community:'虚构测试社区',task_state:'unchecked',summary:{title:`虚构任务${String(e.id).padStart(2,'0')}`,original_address:`虚构测试路${e.id}号 · 仅用于界面验收的长地址，不代表真实地点`},address_match:{status:e.status,small_community_id:e.selected,small_community_name:e.selected?(e.selected===1?'虚构小区甲':'虚构小区乙'):'',candidates:[],reason:'暂无算法候选'}})
await context.route('**/*',async r=>{
 const u=new URL(r.request().url()); if(u.origin!=='http://127.0.0.1:5197')return r.abort(); if(!u.pathname.startsWith('/api/'))return r.continue()
 const p=decodeURIComponent(u.pathname), send=(v,status=200)=>r.fulfill({status,contentType:'application/json',body:JSON.stringify(v)})
 if(p==='/api/app/bootstrap')return send({environment:'production',server_version:'0.28.9',timezone:'Asia/Shanghai'})
 if(p==='/api/auth/me')return send({user:{id:999,username:'synthetic-ux',display_name:'虚构验收账号',role:'super_admin',permissions:['online.raw.view','online.raw.edit','online.task.manage'],permission_groups:[],member:{position:'基础管控',name:'虚构验收账号'},preferences:{}}})
 if(p.endsWith('/filter-options'))return send({communities:[{value:'虚构测试社区',label:'虚构测试社区'}]})
 if(p.endsWith('/search')&&p.includes('/mobile-tasks/')){const b=r.request().postDataJSON(),rows=entries.filter(e=>!b.match_status?.length||b.match_status.includes(e.status)).filter(e=>!b.keyword||task(e).summary.title.includes(b.keyword));return send({data:rows.slice((b.page-1)*20,b.page*20).map(task),total:rows.length,page:b.page,page_size:20,source_ready:true})}
 const id=Number(p.match(/fixture-(\d+)/)?.[1]);if(id){const e=entries[id-1]
 if(p.endsWith('/address-match/options')){ if(optionFailure){optionFailure=false;return send({detail:'虚构选项失败'},503)};return send({items:[{id:1,name:'虚构小区甲',community_id:1,community_name:'虚构测试社区'},{id:2,name:'虚构小区乙',community_id:1,community_name:'虚构测试社区'}],total:2,community:'虚构测试社区',capabilities:{confirm:!readonly,manual_unmatched:!readonly,resolve_conflict:!readonly},history:[],manual_unmatched_reason:e.status==='manual_unmatched'?'community_registry_missing':''})}
 if(r.request().method()==='POST'){if(failNext){const status=failNext;failNext=0;return send({detail:'虚构失败，请重试'},status)}e.status=p.endsWith('manual-unmatched')?'manual_unmatched':'confirmed';e.selected=e.status==='confirmed'?r.request().postDataJSON().small_community_id:null;e.revision++;return send({message:'已保存',address_match:task(e).address_match,task:task(e)})}
 if(id===delayedId)await new Promise(resolve=>setTimeout(resolve,600));
 return send({task:task(e),address_match:task(e).address_match,sources:[{id,row_key:`fixture-${id}`,revision:e.revision,row_hash:'a'.repeat(64),values:{'现住址':'虚构现住址，仅用于验收'}}],workflow:{columns:[]},events:[]})}
 if(p.includes('notifications'))return send({data:[],total:0,unread_count:0})
 return send({data:[],total:0,items:[],unavailable_sources:[],summary:{active_count:0,attention_count:0}})
})
try {
 await page.goto('http://127.0.0.1:5197/address-confirmation?parser_type=全链条&row_key=fixture-1')
 await page.getByRole('heading',{name:'虚构任务01',exact:true}).waitFor()
 await page.evaluate(()=>{const state=history.state;history.replaceState({...state,idx:0},'', '/previous-fixture');history.pushState({...state,idx:1},'', '/address-confirmation?parser_type=全链条&row_key=fixture-1')})
 await page.reload()
 await page.getByRole('heading',{name:'虚构任务01',exact:true}).waitFor()
 await page.getByRole('radio',{name:'虚构小区乙',exact:false}).check()
 const dialog=page.waitForEvent('dialog');await page.evaluate(()=>history.back());await (await dialog).dismiss()
 await page.waitForTimeout(200)
 assert.ok(page.url().includes('address-confirmation'))
 assert.ok(await page.getByRole('radio',{name:'虚构小区乙',exact:false}).isChecked())
 await page.getByRole('button',{name:'确认并停留',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('.address-review-heading')?.textContent.includes('已确认'))
 for(const [width,height] of [[1024,640],[1280,720],[1280,960],[1680,1050],[1920,1080],[390,844]]) {
  await page.setViewportSize({width,height})
  for(const mode of ['light','dark']) {
   await page.evaluate(mode=>localStorage.setItem('binhu-theme-mode',mode),mode)
   await page.reload()
   await page.getByRole('heading',{name:'虚构任务01',exact:true}).waitFor()
   await page.getByRole('button',{name:'确认并停留',exact:true}).scrollIntoViewIfNeeded()
   await page.getByRole('button',{name:'确认并停留',exact:true}).click({trial:true})
   await page.waitForTimeout(150)
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width}`)
   await page.screenshot({path:`${out}/shell-${width}x${height}-${mode}.png`,fullPage:true})
  }
 }
 // 125% scaling equivalent available CSS area, with the navigation sidebar collapsed.
 await page.setViewportSize({width:1536,height:864})
 await page.reload();await page.getByRole('heading',{name:'虚构任务01',exact:true}).waitFor()
 const expand=page.getByRole('button',{name:'展开侧边栏',exact:true});if(await expand.count())await expand.click()
 await page.getByRole('button',{name:'收起侧边栏',exact:true}).click()
 await page.screenshot({path:`${out}/shell-125-percent-collapsed.png`,fullPage:true})
 assert.deepEqual(errors,[])
 console.log('PASS app shell, browser-back draft cancellation, exact task save, 12 viewport/themes and collapsed sidebar')
} catch(e) {
 await page.screenshot({path:`${out}/shell-failure.png`,fullPage:true});console.log(errors);throw e
} finally { await browser.close() }
