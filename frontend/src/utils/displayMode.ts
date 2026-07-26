/** 获取表格显示模式（'table' | 'card'），默认 'table' */
export function getDisplayMode(): 'table' | 'card' {
  if (typeof window === 'undefined') return 'table'
  return (localStorage.getItem('table_display_mode') as 'table' | 'card') || 'table'
}
