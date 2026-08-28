import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  contributionDateLabel,
  contributionDaysForYear,
  contributionLevel,
} from '../src/utils/contributionCalendar.ts'
import { defaultMobileDockConfig } from '../src/navigation/mobileNavigation.ts'
import { resolveApiAssetUrl } from '../src/utils/apiUrl.ts'

test('贡献强度使用固定工作量区间', () => {
  assert.deepEqual(
    [0, 1, 2, 3, 4, 7, 8, 20].map(contributionLevel),
    [0, 1, 2, 2, 3, 3, 4, 4],
  )
})

test('profile upload and light theme contrast', () => {
  const profile = readFileSync(new URL('../src/pages/Profile.tsx', import.meta.url), 'utf8')
  const workbench = readFileSync(new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url), 'utf8')
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')
  assert.match(profile, /uploadAvatar\(file\)/)
  assert.match(profile, /beforeUpload=\{handleAvatarUpload\}/)
  assert.match(profile, /loading=\{avatarUploading\}/)
  assert.match(profile, /user\.avatar_url \? '更换头像' : '上传头像'/)
  assert.match(profile, /user\.avatar_url/)
  assert.match(workbench, /police-dispatch-workbench__hero/)
  assert.doesNotMatch(workbench, /bg-gradient-to-br from-blue-700/)
  assert.match(styles, /police-dispatch-workbench__hero[\s\S]*var\(--app-surface\)/)
})

test('desktop clients resolve private avatar paths against the configured API server', () => {
  assert.equal(
    resolveApiAssetUrl('/api/auth/avatar/7?v=abc', 'https://example.test/api'),
    'https://example.test/api/auth/avatar/7?v=abc',
  )
  assert.equal(
    resolveApiAssetUrl('/api/auth/avatar/7?v=abc', ''),
    '/api/auth/avatar/7?v=abc',
  )
  assert.equal(
    resolveApiAssetUrl('https://cdn.example.test/avatar.jpg', 'https://example.test/api'),
    'https://cdn.example.test/avatar.jpg',
  )
})

test('账号区域优先显示已上传头像并保留默认图标兜底', () => {
  const layout = readFileSync(new URL('../src/components/Layout.tsx', import.meta.url), 'utf8')
  const authenticatedImage = readFileSync(new URL('../src/components/AuthenticatedImage.tsx', import.meta.url), 'utf8')
  const authenticatedImageHook = readFileSync(new URL('../src/hooks/useAuthenticatedImageUrl.ts', import.meta.url), 'utf8')
  assert.match(layout, /<AuthenticatedAvatar[\s\S]*src=\{user\.avatar_url\}/)
  assert.match(layout, /icon=\{<UserOutlined \/>\}/)
  assert.match(layout, /getUserDisplayName\(user\)\.slice\(0, 1\)/)
  assert.match(authenticatedImage, /useAuthenticatedImageUrl/)
  assert.match(authenticatedImageHook, /URL\.createObjectURL/)
  assert.match(authenticatedImageHook, /URL\.revokeObjectURL/)
})

test('热力图只接收所选年度日期并保留年度边界', () => {
  assert.deepEqual(contributionDaysForYear([
    { date: '2025-12-31', count: 8 },
    { date: '2026-01-01', count: 1 },
    { date: '2026-12-31', count: 2 },
    { date: '2027-01-01', count: 3 },
    { date: '2026-06-01', count: 0 },
  ], 2026), [
    { date: '2026-01-01', count: 1 },
    { date: '2026-12-31', count: 2 },
  ])
  assert.equal(contributionDateLabel('2026/8/6'), '2026年8月6日')
})

test('不再提供独立人员主页导航入口', () => {
  for (const role of ['member', 'leader', 'admin', 'super_admin'] as const) {
    const config = defaultMobileDockConfig(role)
    assert.equal(
      config.groups.some(group => (group.items as string[]).includes('people')),
      false,
      role,
    )
  }
})

test('人员管理姓名进入个人资料且旧目录地址回到人员管理', () => {
  const membersSource = readFileSync(
    new URL('../src/pages/GridMembers.tsx', import.meta.url),
    'utf8',
  )
  const appSource = readFileSync(
    new URL('../src/App.tsx', import.meta.url),
    'utf8',
  )
  assert.match(membersSource, /navigate\(`\/people\/\$\{member\.account\?\.id\}`,[\s\S]*returnTo: '\/grid-members'/)
  assert.match(appSource, /path="\/people" element=\{<Navigate to="\/grid-members" replace \/>\}/)
  assert.equal(appSource.includes('PeopleDirectory'), false)
})

test('人员管理电话对所有页面查看者显示但备注仍受敏感权限控制', () => {
  const source = readFileSync(
    new URL('../src/pages/GridMembers.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /canViewSensitive \|\| column\.key !== 'notes'/)
  assert.equal(source.includes("['phone', 'notes']"), false)
  assert.match(source, /<span className="w-16 shrink-0 text-slate-500">电话<\/span>[\s\S]*\{member\.phone \|\| '-'\}/)
  assert.equal(source.includes('{canViewSensitive && <div className="flex min-w-0 gap-3">\n          <span className="w-16 shrink-0 text-slate-500">电话</span>'), false)
})

test('缺少手机号的下发任务显示为待研判', () => {
  const workbenchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const batchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchBatchDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(workbenchSource, /manual: '待研判'/)
  assert.match(workbenchSource, /item\.suggested_action === 'manual'/)
  assert.match(batchSource, /manual: '待研判'/)
})

test('公开个人主页不渲染用户名或敏感字段', () => {
  const source = readFileSync(
    new URL('../src/pages/PublicProfile.tsx', import.meta.url),
    'utf8',
  )
  for (const forbidden of ['username', '身份证号', '手机号', 'permission_groups']) {
    assert.equal(source.includes(forbidden), false, forbidden)
  }
  assert.match(source, /public-profile-layout/)
  assert.match(source, /returnState\?\.returnTo \|\| '\/'/)
  assert.match(source, /returnState\?\.returnLabel \|\| '返回仪表盘'/)
})

test('登录错误提示与按钮使用独立间距容器', () => {
  const source = readFileSync(
    new URL('../src/pages/Login.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /<div className="grid gap-3 pt-1">[\s\S]*<Alert[\s\S]*<Button/)
})

test('登录页只在成功后记录本地历史账号且不持久化密码', () => {
  const source = readFileSync(
    new URL('../src/pages/Login.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /readRememberedUsername\(storage\)/)
  assert.match(source, /readRememberedUsernames\(storage\)/)
  assert.match(source, /await login\(normalizedUsername, password\)[\s\S]*storeRememberedUsername\(storage, normalizedUsername\)/)
  assert.match(source, /clearRememberedUsername\(storage\)/)
  assert.match(source, /<AutoComplete[\s\S]*options=\{rememberedUsernames\.map/)
  assert.match(source, />\s*记住账号\s*<\/Checkbox>/)
  assert.doesNotMatch(source, /storeRememberedUsername\([^\n]*password/)
})

test('客户端版本来自本地构建且登录页和侧栏显示同一版本', () => {
  const auth = readFileSync(new URL('../src/context/AuthContext.tsx', import.meta.url), 'utf8')
  const login = readFileSync(new URL('../src/pages/Login.tsx', import.meta.url), 'utf8')
  const layout = readFileSync(new URL('../src/components/Layout.tsx', import.meta.url), 'utf8')
  assert.match(auth, /clientVersion: typeof __APP_VERSION__ === 'string' \? __APP_VERSION__ : '0\.0\.0'/)
  assert.match(auth, /if \(payload\.server_version\) setServerVersion\(payload\.server_version\)/)
  assert.match(login, /客户端版本 v\{clientVersion\}/)
  assert.match(layout, />v\{clientVersion\}<\/div>/)
  assert.doesNotMatch(layout, /数据管理中心 · v\{clientVersion\}/)
  assert.doesNotMatch(layout, /数据管理中心 · v\{serverVersion\}/)
})

test('登录页使用公安平台品牌文案和分层视觉素材', () => {
  const source = readFileSync(
    new URL('../src/pages/Login.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /守护平安滨湖/)
  assert.match(source, /滨湖公安智慧平台/)
  assert.match(source, /loginBlueGrid/)
  assert.match(source, /loginSilkCity/)
  assert.match(source, /policeEmblem/)
  assert.equal(source.includes('守护滨湖平安'), false)
  assert.equal(source.includes('滨湖新城智慧平台'), false)
})

test('登录页右侧表单区域跟随深色主题', () => {
  const styles = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )
  assert.match(styles, /html\[data-theme='dark'\] \.login-form-panel/)
  assert.match(styles, /html\[data-theme='dark'\] \.login-form-card/)
  assert.match(styles, /html\[data-theme='dark'\] \.login-form__field \.ant-input-affix-wrapper/)
  assert.match(styles, /html\[data-theme='dark'\] \.login-product-brand h2/)
  assert.match(styles, /html\[data-theme='dark'\] \.login-form__field input:-webkit-autofill/)
})
