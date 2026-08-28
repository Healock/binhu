import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('户号表与确认接口保留长请求超时，告知书改为后台任务', () => {
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /api\.post\('\/registry\/imports\/households\/preview', form, \{[\s\S]*?timeout: 300_000/)
  assert.match(apiSource, /api\.post\(`\/registry\/imports\/households\/\$\{batchId\}\/confirm`, \{\}, \{[\s\S]*?timeout: 300_000/)
  assert.match(apiSource, /api\.post\(`\/registry\/imports\/certificates\/\$\{batchId\}\/confirm`, \{\}, \{[\s\S]*?timeout: 300_000/)
  assert.match(apiSource, /api\.post\('\/registry\/imports\/certificates\/source-runs', \{\}/)
  assert.match(apiSource, /api\.get\('\/registry\/imports\/certificates\/source-runs\/latest'\)/)
  assert.match(apiSource, /api\.get\(`\/registry\/imports\/certificates\/source-runs\/\$\{runId\}`\)/)
  assert.match(apiSource, /source-runs\/\$\{runId\}\/retry/)
})

test('辖区档案页面显示告知书后台进度并允许断点继续', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /reason\?\.response\?\.status === 413/)
  assert.match(pageSource, /户号表超过服务器当前上传限制/)
  assert.match(pageSource, /已保存至第 \{certificateRun\.current_page\} 页/)
  assert.match(pageSource, /继续读取/)
  assert.match(pageSource, /重新读取/)
  assert.match(pageSource, /可以离开本页面，任务会在服务器继续执行/)
  assert.doesNotMatch(pageSource, /告知书读取超时/)
})

test('房屋档案和问题核查使用正文搜索并提供完整筛选', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /api\.post\('\/registry\/properties\/search', params/)
  assert.match(apiSource, /api\.post\('\/registry\/import\/issues\/search', params/)
  assert.match(pageSource, /搜索地址、户号、幢室或住房类型/)
  assert.match(pageSource, /全部社区/)
  assert.match(pageSource, /出租房/)
  assert.match(pageSource, /自购房/)
  assert.match(pageSource, /全部住房类型/)
  assert.match(pageSource, /pagination=\{listPagination\}/)
})

test('小区管理支持工地宿舍类型，辖区档案按社区提交筛选', () => {
  const addressPageSource = readFileSync(
    new URL('../src/pages/PoliceAddressManagement.tsx', import.meta.url),
    'utf8',
  )
  const registryPageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(addressPageSource, /construction_dormitory/)
  assert.match(addressPageSource, /工地宿舍/)
  assert.match(apiSource, /address_type: 'community' \| 'apartment' \| 'construction_dormitory' \| 'other'/)
  assert.match(registryPageSource, /全部社区/)
  assert.match(registryPageSource, /community_id: communityId/)
})

test('问题数据核查说明外部修正原因并显示字段和错误值', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /到居住证系统更新正确内容/)
  assert.match(pageSource, /问题字段与错误值/)
  assert.match(pageSource, /为什么有问题/)
  assert.match(pageSource, /registry-issue-evidence__value/)
  assert.doesNotMatch(pageSource, /标记已核查/)
  assert.doesNotMatch(pageSource, /reviewImportIssue/)
  assert.match(pageSource, /key: 'imports', label: '数据导入'/)
})

test('房屋档案突出显示责任书状态和责任关系', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /certificate_status\?: RegistryCertificateStatus/)
  assert.match(pageSource, /全部责任书状态/)
  assert.match(pageSource, /无需上传告知书/)
  const propertyColumns = pageSource.slice(
    pageSource.indexOf('const propertyColumns'),
    pageSource.indexOf('const personColumns'),
  )
  assert.doesNotMatch(propertyColumns, /title: '幢'/)
  assert.doesNotMatch(propertyColumns, /title: '室'/)
  assert.match(pageSource, /registry-certificate-summary--\$\{summary\.certificate_status\}/)
  assert.match(pageSource, /实际出租人未确定/)
  assert.match(pageSource, /责任身份/)
  assert.match(pageSource, /最近来源读取/)
})

test('房屋详情按档案导入权限提供责任告知书图片预览', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /registry\/properties\/\$\{propertyId\}\/certificates\/\$\{certificateId\}\/image/)
  assert.match(apiSource, /responseType: 'blob'/)
  assert.match(pageSource, /查看责任告知书/)
  assert.match(pageSource, /来源未提供图片/)
  assert.match(pageSource, /certificateImageLoading/)
  assert.match(pageSource, /URL\.createObjectURL\(blob\)/)
  assert.match(pageSource, /Image src=\{certificatePreview\.url\}/)
})

test('房屋档案展示最近走访并按页读取历史走访和星级', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /registry\/properties\/\$\{id\}\/visits/)
  assert.match(apiSource, /latest_visit_date: string \| null/)
  assert.match(pageSource, /title: '最近走访日期'/)
  const propertyColumns = pageSource.slice(
    pageSource.indexOf('const propertyColumns'),
    pageSource.indexOf('const personColumns'),
  )
  assert.doesNotMatch(propertyColumns, /累计 \$\{row\.visit_count\} 次/)
  assert.match(propertyColumns, /title: '星级评定'/)
  assert.match(pageSource, /历史走访与星级评定/)
  assert.match(pageSource, /onChange: nextPage => void loadPropertyVisits\(nextPage\)/)
  assert.match(pageSource, /title: '星级评定'/)
})

test('人员标签整合到人员档案并保留独立权限和历史', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )
  const appSource = readFileSync(
    new URL('../src/App.tsx', import.meta.url),
    'utf8',
  )
  const navigationSource = readFileSync(
    new URL('../src/navigation/mobileNavigation.ts', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /registry\.watch\.view/)
  assert.match(pageSource, /registry\.watch\.manage/)
  assert.match(pageSource, /按人员标签筛选/)
  assert.match(pageSource, /title="人员标签"/)
  assert.match(pageSource, /标签历史|tag_assignments/)
  assert.match(pageSource, /releasePersonTag/)
  assert.match(apiSource, /people\/\$\{id\}\/tags\/\$\{assignmentId\}\/release/)
  assert.match(appSource, /path="\/watch-people" element=\{<Navigate to="\/registry" replace \/>\}/)
  assert.doesNotMatch(appSource, /import WatchPeopleManagement/)
  assert.doesNotMatch(navigationSource, /id: 'watch_people'/)
})
