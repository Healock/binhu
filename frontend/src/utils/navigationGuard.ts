let pendingChanges = false

export function setPendingNavigationChanges(value: boolean): void {
  pendingChanges = value
}

export function confirmPendingNavigation(): boolean {
  return !pendingChanges || window.confirm('当前修改尚未保存，确定离开吗？')
}
