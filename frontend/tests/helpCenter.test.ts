import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'


const page = readFileSync(new URL('../src/pages/HelpCenter.tsx', import.meta.url), 'utf8')
const api = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
const navigation = readFileSync(
  new URL('../src/navigation/mobileNavigation.ts', import.meta.url),
  'utf8',
)
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')


test('帮助中心使用 Markdown 安全渲染并支持分类目录和搜索', () => {
  assert.match(page, /ReactMarkdown/)
  assert.match(page, /remarkPlugins=\{\[remarkGfm\]\}/)
  assert.doesNotMatch(page, /dangerouslySetInnerHTML/)
  assert.match(page, /help-center-catalog/)
  assert.match(page, /item\.title.*item\.category.*item\.summary/s)
  assert.match(page, /noopener/)
})


test('帮助中心对登录用户开放且超级管理员能力由后端返回', () => {
  assert.match(app, /path="\/help"/)
  assert.match(navigation, /id: 'help'[\s\S]*?path: '\/help'/)
  assert.match(page, /document\.can_edit/)
  assert.match(api, /listHelpDocuments/)
  assert.match(api, /getHelpDocument/)
})


test('在线编辑提供预览、乐观锁保存和带版本校验的恢复', () => {
  assert.match(page, /Markdown 编辑/)
  assert.match(page, /label: '预览'/)
  assert.match(page, /expected_revision: document\.revision/)
  assert.match(page, /resetHelpDocument\(document\.slug, document\.revision\)/)
  assert.match(api, /expected_revision: expectedRevision/)
  assert.match(page, /已被其他管理员更新|apiErrorMessage/)
})


test('帮助中心在窄窗口回到单列且编辑区保留字数空间', () => {
  assert.match(styles, /@media \(max-width: 900px\)[\s\S]*?\.help-center-layout\s*\{[\s\S]*?grid-template-columns: 1fr/)
  assert.match(styles, /\.help-editor-textarea[\s\S]*?padding-bottom: 28px/)
  assert.match(styles, /@media \(max-width: 520px\)[\s\S]*?\.help-center-document/)
})
