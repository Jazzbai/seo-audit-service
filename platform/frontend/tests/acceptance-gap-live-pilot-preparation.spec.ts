import { expect, test, type Page } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

// Included in the default deterministic acceptance-gap UI suite.
const source = 'https://workshop.example/contact/'
async function mockPlatform(page: Page, role = 'owner', rejectReview = false) {
  const requests: { reviews: any[]; policies: any[] } = { reviews: [], policies: [] }
  const site = { id: 'pilot-site', name: 'Isolated workshop', origin: 'https://workshop.example', paused: true, facts: {} }
  let article: any = { id:'pilot-article', site_id:site.id, title:'Prepare for your estimate', body:'<p>Ask the workshop about intake.</p>', sources:[source], status:'review_needed', managed:true, author_id:'1', updated_at:'2026-09-24T22:00:00Z', brief:{generation:{kind:'provider_generation',unverified_sources:[{url:source}]}}, checks:{passed:false,blockers:['unverified_sources'],warnings:[]},source_review_state:{accepted_urls:[],valid_for_days:7} }
  let policy: any = { id:'policy',version:2,settings:{enabled:false,allowed_actions:[],protected_paths:['/','/services*'],posts_per_week:1,refreshes_per_week:0,monthly_budget_cents:30000,publish_days:[],author_id:'1',tracked_keywords:[],competitors:[],tracked_questions:[],publication_article_ids:null} }
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1','')
    const method = route.request().method()
    let body: any = {items:[],total:0}
    let status = 200
    if(path==='/auth/status') body={initialized:true}
    if(path==='/auth/me') body={user:{id:'editor',name:'Editor',email:'editor@example.test'},team:{id:'team',name:'Fixture'},role,csrf_token:'fixture-token'}
    if(path==='/settings') body={global_pause:true}
    if(path==='/sites') body={items:[site],total:1}
    if(path==='/sites/pilot-site') body=site
    if(path.endsWith('/connections')) body={items:[{kind:'wordpress',status:'connected',capabilities:{authenticated:true,authenticated_author:{id:'1',name:'Fixture author'},native:{create:true,publish:true}}}],total:1}
    if(path.endsWith('/budgets')) body={accounts:{items:[],total:0},reservations:{items:[],total:0}}
    if(path==='/sites/pilot-site/policy') {
      if(method==='PUT') { requests.policies.push(route.request().postDataJSON()); policy={...policy,version:policy.version+1,...route.request().postDataJSON()} }
      body=policy
    }
    if(path==='/sites/pilot-site/articles') body={items:[article,{...article,id:'other-article',title:'An unrelated draft'}],total:2}
    if(path==='/sites/pilot-site/articles/pilot-article') body=article
    if(path.endsWith('/source-reviews')) {
      requests.reviews.push(route.request().postDataJSON())
      if(rejectReview) { status=409;body={detail:'The draft changed; reload before reviewing its sources'} }
      else {
        article={...article,status:'checked',updated_at:'2026-09-24T22:01:00Z',checks:{passed:true,blockers:[],warnings:[]},source_review_state:{accepted_urls:[source],valid_for_days:7}}
        body=article
      }
    }
    await route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)})
  })
  return requests
}

for(const mobile of [false,true]) test(`source review needs an explicit explanation and confirmation (${mobile?'mobile':'desktop'})`,async({page})=>{
  if(mobile)await page.setViewportSize({width:390,height:844})
  const requests=await mockPlatform(page)
  await page.goto('/sites/pilot-site/content/articles/pilot-article')
  await page.getByLabel('Source to review').selectOption(source)
  const button=page.getByRole('button',{name:'Record source review'})
  await expect(button).toBeDisabled()
  await page.getByLabel('Source review notes').fill('The contact page supports directing readers to ask the shop about its current intake process.')
  await expect(button).toBeDisabled()
  await page.getByLabel('I reviewed this source against the saved article').check()
  await button.click()
  await expect(page.getByText('Review is current for this saved revision.')).toBeVisible()
  expect(requests.reviews).toHaveLength(1)
  expect(requests.reviews[0].expected_updated_at).toBe('2026-09-24T22:00:00Z')
  expect(requests.reviews[0].confirms_claim_support).toBe(true)
  await expect(page.getByText('No blockers or warnings were returned.')).toBeVisible()
  const audit=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa']).analyze()
  expect(audit.violations).toEqual([])
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true)
})

test('unsaved content disables source review',async({page})=>{
  const requests=await mockPlatform(page)
  await page.goto('/sites/pilot-site/content/articles/pilot-article')
  await page.getByLabel('Body',{exact:false}).fill('<p>Unsaved new claim.</p>')
  await expect(page.getByRole('button',{name:'Record source review'})).toBeDisabled()
  await expect(page.getByText('Save any article changes before reviewing its sources.')).toBeVisible()
  expect(requests.reviews).toHaveLength(0)
})

test('stale-source review failure is visible and never claimed successful',async({page})=>{
  await mockPlatform(page,'owner',true)
  await page.goto('/sites/pilot-site/content/articles/pilot-article')
  await page.getByLabel('Source to review').selectOption(source)
  await page.getByLabel('Source review notes').fill('The page supports the contact link used for the shop intake question.')
  await page.getByLabel('I reviewed this source against the saved article').check()
  await page.getByRole('button',{name:'Record source review'}).click()
  await expect(page.getByText('The draft changed; reload before reviewing its sources')).toBeVisible()
  await expect(page.getByText('Review is current for this saved revision.')).toHaveCount(0)
})

test('scope selects one named draft while preserving pauses and protected paths',async({page})=>{
  const requests=await mockPlatform(page)
  await page.goto('/sites/pilot-site/settings/policies')
  await page.getByLabel('Restrict publishing to selected articles').check()
  await expect(page.getByText('No articles selected: publication is blocked for every article.')).toBeVisible()
  await page.getByLabel('Prepare for your estimate (review_needed)').check()
  await page.getByRole('button',{name:'Save policy controls'}).click()
  await expect(page.getByText('Policy and pause controls saved as new server state.')).toBeVisible()
  expect(requests.policies[0].settings.publication_article_ids).toEqual(['pilot-article'])
  expect(requests.policies[0].settings.enabled).toBe(false)
  expect(requests.policies[0].settings.allowed_actions).toEqual([])
  expect(requests.policies[0].settings.protected_paths).toEqual(['/','/services*'])
  await expect(page.getByRole('button',{name:'Toggle site pause'})).toHaveAttribute('aria-pressed','true')
  await expect(page.getByRole('button',{name:'Toggle global pause'})).toHaveAttribute('aria-pressed','true')
})

test('viewer sees scope but cannot change it or record a source review',async({page})=>{
  await mockPlatform(page,'viewer')
  await page.goto('/sites/pilot-site/content/articles/pilot-article')
  await expect(page.getByRole('button',{name:'Record source review'})).toBeDisabled()
  await page.goto('/sites/pilot-site/settings/policies')
  await expect(page.getByLabel('Restrict publishing to selected articles')).toBeDisabled()
})
