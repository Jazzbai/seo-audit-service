import { expect, test, type Page, type Route } from '@playwright/test'

const site = { id: 'site-1', team_id: 'team-1', name: 'Pilot site', origin: 'https://pilot.example', timezone: 'America/Chicago', language: 'en', paused: true, facts: {} }

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installTeamMocks(page: Page, role: 'owner' | 'viewer' = 'owner') {
  const currentUser = role === 'owner'
    ? { id: 'user-1', email: 'owner@example.com', name: 'Owner' }
    : { id: 'user-2', email: 'viewer@example.com', name: 'Viewer' }
  let members = [
    { id: 'user-1', email: 'owner@example.com', name: 'Owner', role: 'owner' },
    { id: 'user-2', email: 'viewer@example.com', name: 'Viewer', role: 'viewer' },
  ]
  const changes: Array<{ method: string; path: string; body?: unknown }> = []
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()
    if (path === '/auth/status') return json(route, { initialized: true })
    if (path === '/auth/me') return json(route, { user: currentUser, team: { id: 'team-1', name: 'Pilot team' }, role, csrf_token: 'csrf-test-token' })
    if (path === '/sites' && method === 'GET') return json(route, { items: [site], total: 1 })
    if (path === '/team' && method === 'GET') return json(route, { team: { id: 'team-1', name: 'Pilot team' }, members })
    const memberMatch = path.match(/^\/team\/members\/([^/]+)$/)
    if (memberMatch && method === 'PATCH') {
      const body = request.postDataJSON() as { role: string }
      changes.push({ method, path, body })
      members = members.map((member) => member.id === memberMatch[1] ? { ...member, role: body.role } : member)
      return json(route, { member: members.find((member) => member.id === memberMatch[1]) })
    }
    if (memberMatch && method === 'DELETE') {
      changes.push({ method, path })
      members = members.filter((member) => member.id !== memberMatch[1])
      return json(route, { ok: true, user_id: memberMatch[1] })
    }
    if (path === '/team/members' && method === 'POST') return json(route, { member: {} })
    return json(route, { items: [], total: 0 })
  })
  return { changes }
}

test('owner can change a role and remove a member while their own controls stay protected', async ({ page }) => {
  const controls = await installTeamMocks(page)
  await page.goto('/sites/site-1/settings/team')

  await expect(page.getByRole('heading', { name: 'Team members' })).toBeVisible()
  const ownerRole = page.getByLabel('Role for owner@example.com')
  await expect(ownerRole).toBeDisabled()
  await page.getByLabel('Role for viewer@example.com').selectOption('editor')
  await expect(page.getByLabel('Role for viewer@example.com')).toHaveValue('editor')
  expect(controls.changes).toEqual(expect.arrayContaining([{ method: 'PATCH', path: '/team/members/user-2', body: { role: 'editor' } }]))

  page.once('dialog', (dialog) => void dialog.accept())
  await page.getByTitle('Remove this member').click()
  await expect(page.getByText('Team member removed.')).toBeVisible()
  await expect(page.getByText('viewer@example.com')).toHaveCount(0)
  expect(controls.changes).toEqual(expect.arrayContaining([{ method: 'DELETE', path: '/team/members/user-2' }]))
})

test('viewer sees read-only team controls', async ({ page }) => {
  await installTeamMocks(page, 'viewer')
  await page.goto('/sites/site-1/settings/team')

  await expect(page.getByText('Only the owner can add or change team members.')).toBeVisible()
  await expect(page.getByLabel('Role for viewer@example.com')).toBeDisabled()
  await expect(page.getByLabel('Name')).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Add member' })).toBeDisabled()
  await expect(page.getByTitle('Remove this member')).toBeDisabled()
})
