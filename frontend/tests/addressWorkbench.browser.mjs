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
const address=id=>`虚构测试路${id}号 · 仅用于界面验收的长地址，不代表真实地点`
const task=e=>({task_key:`全链条:fixture-${e.id}`,parser_type:'全链条',row_key:`fixture-${e.id}`,community:'虚构测试社区',task_state:'unchecked',summary:{title:`虚构任务${String(e.id).padStart(2,'0')}`,original_address:e.addressOverride ?? address(e.id)},address_match:{status:e.status,small_community_id:e.selected,small_community_name:e.selected?(e.selected===1?'虚构小区甲':'虚构小区乙'):'',candidates:[],reason:'暂无算法候选'}})
await context.route('**/*',async r=>{
 const u=new URL(r.request().url()); if(u.origin!=='http://127.0.0.1:5197')return r.abort(); if(u.pathname==='/address-confirmation')return r.fulfill({contentType:'text/html',body:'<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script type="module" src="/tests/addressWorkbench.fixture.tsx"></script></body></html>'}); if(!u.pathname.startsWith('/api/'))return r.continue()
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
 return send({data:[],total:0})
})
try{
 await page.goto('http://127.0.0.1:5197/tests/addressWorkbench.fixture.html?parser_type=全链条')
 await page.locator('.address-queue-row').first().click()
 await page.getByRole('heading',{name:address(1),exact:true}).waitFor()
 assert.ok(!(await page.locator('body').innerText()).includes('虚构任务'), 'task subjects must not be rendered')
 await page.screenshot({path:`${out}/initial.png`,fullPage:true})
 for(let i=1;i<=23;i++){await page.getByRole('heading',{name:address(i),exact:true}).waitFor();if(i===2){await page.locator('.address-saved').getByRole('button',{name:'查看'}).click();await page.getByRole('heading',{name:address(1),exact:true}).waitFor();await page.getByRole('button',{name:'返回刚才正在核对的任务'}).click();await page.getByRole('heading',{name:address(2),exact:true}).waitFor()}await page.getByRole('radio',{name:'虚构小区甲',exact:false}).check();await page.getByRole('button',{name:'确认并下一条',exact:true}).click()}
 await page.getByText('当前筛选已处理完，最后一条结果保留在此').waitFor();assert.equal(entries.filter(e=>e.status==='confirmed').length,23)
 await page.locator('.address-saved').getByRole('button',{name:'查看'}).click()
 await page.getByRole('heading',{name:address(23),exact:true}).waitFor()
 await page.getByRole('radio',{name:'无匹配小区',exact:true}).check();await page.locator('#annotation-reason').click();await page.getByText('小区库缺失',{exact:true}).click()
 failNext=500;await page.getByRole('button',{name:'确认并停留',exact:true}).click();await page.getByText('虚构失败，请重试',{exact:true}).first().waitFor();assert.equal(entries[22].status,'confirmed')
 await page.getByRole('button',{name:'确认并停留',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.address-review-heading')?.textContent.includes('无匹配小区'))


 // Rapid selection: a late response for task 2 must not overwrite task 3.
 entries[1].status='unmatched'; entries[2].status='unmatched'
 await page.goto('http://127.0.0.1:5197/tests/addressWorkbench.fixture.html?parser_type=全链条')
 delayedId=2
 await page.locator('.address-queue-row').filter({hasText:address(2)}).click()
 await page.locator('.address-queue-row').filter({hasText:address(3)}).click()
 await page.getByRole('heading',{name:address(3),exact:true}).waitFor()
 await page.waitForTimeout(700)
 assert.equal(await page.locator('.address-review-heading h2').innerText(),address(3))
 delayedId=0
 // An exact confirmed-task link must open despite the pending filter and default to stay.
 await page.goto('http://127.0.0.1:5197/tests/addressWorkbench.fixture.html?parser_type=全链条&row_key=fixture-1')
 await page.getByRole('heading',{name:address(1),exact:true}).waitFor()
 await page.locator('.address-submit .ant-btn-primary').filter({hasText:'确认并停留'}).waitFor()
 await page.getByRole('radio',{name:'虚构小区乙',exact:false}).check()
 failNext=409
 await page.getByRole('button',{name:'确认并停留',exact:true}).click()
 await page.getByRole('button',{name:'重新核对最新版本'}).click()
 await page.getByText('已读取最新版本，请对照地址与社区重新核对保留的选择。确认无误后可再次提交。').waitFor()
 assert.ok(await page.getByRole('radio',{name:'虚构小区乙',exact:false}).isChecked())
 await page.getByRole('button',{name:'确认并停留',exact:true}).click()
 await page.waitForFunction(()=>!document.querySelector('.address-submit')?.textContent.includes('正在保存'))
 assert.equal(entries[0].selected,2)
 // Loading options is independent of detail; retry cannot destroy the selected task.
 optionFailure=true
 await page.reload()
 await page.getByText('虚构选项失败',{exact:true}).waitFor()
 await page.getByRole('button',{name:'重新加载',exact:true}).click()
 await page.getByRole('radio',{name:'虚构小区甲',exact:false}).waitFor()
 // Narrow and full-size rendering uses the real Ant Design theme algorithms.
 for(const [width,height] of [[1024,640],[1280,720],[1280,960],[1680,1050],[1920,1080],[390,844]]){await page.setViewportSize({width,height});await page.waitForTimeout(200);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`overflow ${width}`);await page.screenshot({path:`${out}/${width}x${height}-light.png`,fullPage:true});await page.evaluate(()=>{window.setFixtureTheme(true)});await page.waitForTimeout(150);await page.screenshot({path:`${out}/${width}x${height}-dark.png`,fullPage:true});await page.evaluate(()=>{window.setFixtureTheme(false)})}
 entries[0].addressOverride=''
 await page.reload();await page.getByRole('heading',{name:'未填写原始地址',exact:true}).waitFor()
 assert.ok(!(await page.locator('body').innerText()).includes('虚构任务'), 'empty addresses must not fall back to task subjects')
 readonly=true;await page.reload();await page.getByText('当前账号仅可查看，请联系组长或有权管理任务的上级岗位标注').waitFor();assert.ok(await page.getByRole('radio',{name:'归属具体小区',exact:true}).isDisabled());assert.deepEqual(errors,[]);writeFileSync(`${out}/result.json`,JSON.stringify({passed:true,confirmed:23,manualUnmatched:1,errors},null,2));console.log('PASS 23 continuous tasks, drain, review, failed-save retry, no-match and 12 screenshots')
}catch(e){await page.screenshot({path:`${out}/failure.png`,fullPage:true});writeFileSync(`${out}/failure.txt`,String(e)+'\n'+errors.join('\n')+'\n'+await page.locator('body').innerText());throw e}finally{await browser.close()}
