import test from 'node:test'
import assert from 'node:assert/strict'
import { addressAnnotationUrl, rememberAnnotation, saveRecentAnnotation, readRecentAnnotations, clearRecentAnnotations, PENDING_ADDRESS_STATES } from '../src/utils/addressAnnotation.ts'

test('task links contain only encoded locators, even with reserved characters', () => {
  const url = new URL(addressAnnotationUrl({ parser_type: '测试&类型', row_key: 'a?b/#' }), 'https://fixture.invalid')
  assert.equal(url.pathname, '/address-confirmation')
  assert.deepEqual([...url.searchParams], [['parser_type', '测试&类型'], ['row_key', 'a?b/#']])
})
test('recent review locators survive navigation, are bounded and isolated by account', () => {
  clearRecentAnnotations()
  for (let i=0;i<25;i++) saveRecentAnnotation(1, { parser_type:'虚构类型', row_key:String(i), result:'confirmed' })
  assert.equal(readRecentAnnotations(1).length,20)
  saveRecentAnnotation(1,{parser_type:'虚构类型',row_key:'12',result:'manual_unmatched'})
  assert.equal(readRecentAnnotations(1)[0].row_key,'12')
  assert.equal(readRecentAnnotations(1).filter(item=>item.row_key==='12').length,1)
  const copy=readRecentAnnotations(1);copy.length=0
  assert.equal(readRecentAnnotations(1).length,20)
  assert.deepEqual(readRecentAnnotations(2),[])
  clearRecentAnnotations();assert.deepEqual(readRecentAnnotations(1),[])
})
test('annotation identity includes business type and requires review after input changes', () => {
  const items=rememberAnnotation([{parser_type:'甲',row_key:'same',result:'confirmed'}],{parser_type:'乙',row_key:'same',result:'manual_unmatched'})
  assert.equal(items.length,2)
  assert.ok(PENDING_ADDRESS_STATES.includes('review_required'))
  assert.ok(!PENDING_ADDRESS_STATES.includes('manual_unmatched'))
})
