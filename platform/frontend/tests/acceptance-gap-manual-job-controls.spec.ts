import { expect, test, type Page } from '@playwright/test'

async function mocks(page: Page, options: {paused?:boolean; globalPause?:boolean; failControls?:boolean; role?:string; jobStatus?:string}={}) {
  const site={id:'controls-site',name:'Controls fixture',origin:'https://controls.example.test',timezone:'America/Chicago',language:'en',paused:options.paused??false,facts:{}}
  const article={id:'controls-article',site_id:site.id,title:'A retained draft',body:'<p>Saved draft content.</p>',sources:[],brief:{},status:'checked',author_id:null,checks:{passed:true,blockers:[],warnings:[]}}
  const job={id:'waiting-job',site_id:site.id,kind:'generate',status:options.jobStatus??'queued',result:{},payload:{article_id:article.id},attempts:0}
  let writes=0
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname.replace('/api/v1','')
    const method=route.request().method()
    let value:unknown={items:[],total:0};let status=200
    if(path==='/auth/status')value={initialized:true}
    if(path==='/auth/me')value={user:{id:'owner',name:'Owner',email:'owner@example.test'},team:{id:'team',name:'Team'},role:options.role??'owner',csrf_token:'fixture-csrf'}
    if(path==='/sites')value={items:[site],total:1}
    if(path==='/sites/controls-site')value=site
    if(path==='/settings'){value={global_pause:options.globalPause??false};if(options.failControls){status=503;value={detail:'Workspace controls are unavailable.'}}}
    if(path.endsWith('/authors'))value={items:[],complete:true,blockers:[],warnings:[],authenticated_user_id:'1',checked_at:'2026-10-07T23:00:00Z'}
    if(path.endsWith('/articles/controls-article'))value=article
    if(path==='/sites/controls-site/jobs'&&method==='GET')value={items:[job],total:1}
    if(method==='POST'){writes++;if(path.endsWith('/cancel')){job.status='cancelled';value=job}else value={detail:'Unexpected test mutation'}}
    await route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)})
  })
  return {writes:()=>writes}
}

for(const option of [{paused:true},{globalPause:true}])test(`paused editor explains disabled actions: ${JSON.stringify(option)}`,async({page})=>{
  const state=await mocks(page,option)
  await page.goto('/sites/controls-site/content/articles/controls-article')
  await expect(page.getByText('Automation is paused',{exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'Generate draft',exact:true})).toBeDisabled()
  await expect(page.getByRole('button',{name:'Publish',exact:true})).toBeDisabled()
  await expect(page.getByRole('button',{name:'Save article',exact:true})).toBeEnabled()
  await expect(page.getByRole('button',{name:'Check',exact:true})).toBeEnabled()
  expect(state.writes()).toBe(0)
})

test('unavailable controls fail closed without blocking draft editing',async({page})=>{
  const state=await mocks(page,{failControls:true})
  await page.goto('/sites/controls-site/content/articles/controls-article')
  await expect(page.getByText('Workspace controls are unavailable.',{exact:false})).toBeVisible()
  await expect(page.getByRole('button',{name:'Generate draft',exact:true})).toBeDisabled()
  await expect(page.getByRole('button',{name:'Publish',exact:true})).toBeDisabled()
  await expect(page.getByRole('button',{name:'Save article',exact:true})).toBeEnabled()
  expect(state.writes()).toBe(0)
})

test('verified unpaused controls make generation and publication available',async({page})=>{
  await mocks(page)
  await page.goto('/sites/controls-site/content/articles/controls-article')
  await expect(page.getByRole('button',{name:'Generate draft',exact:true})).toBeEnabled()
  await expect(page.getByRole('button',{name:'Publish',exact:true})).toBeEnabled()
})

test('waiting-job cancellation updates history with a plain-language outcome',async({page})=>{
  const state=await mocks(page)
  page.on('dialog',dialog=>dialog.accept())
  await page.goto('/sites/controls-site/jobs')
  await page.getByRole('button',{name:'Cancel waiting job',exact:true}).click()
  await expect(page.getByText('Waiting job cancelled. No remote changes were undone; future recurring checks are unchanged.',{exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'Cancel waiting job',exact:true})).toHaveCount(0)
  expect(state.writes()).toBe(1)
})

for(const option of [{role:'viewer'},{jobStatus:'running'}])test(`cancellation is not offered without authority or for active work: ${JSON.stringify(option)}`,async({page})=>{
  const state=await mocks(page,option)
  await page.goto('/sites/controls-site/jobs')
  await expect(page.getByRole('heading',{name:'Run history',exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'Cancel waiting job',exact:true})).toHaveCount(0)
  expect(state.writes()).toBe(0)
})
