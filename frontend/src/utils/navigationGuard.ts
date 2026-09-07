let pendingChanges = false
let navigationIndex: number | undefined
let restoringHistory = false
let navigationDecision: (() => boolean) | null = null

export function setNavigationDecision(decision: (() => boolean) | null): void {
  navigationDecision = decision
  navigationIndex = typeof window !== 'undefined' ? window.history.state?.idx : undefined
}

export function setPendingNavigationChanges(value: boolean): void {
  pendingChanges = value
}

export function confirmPendingNavigation(): boolean {
  if (navigationDecision) return navigationDecision()
  return !pendingChanges || window.confirm('当前修改尚未保存，确定离开吗？')
}

// Register before BrowserRouter mounts, so a declined POP cannot render another page.
if (typeof window !== 'undefined') {
  window.addEventListener('popstate', event => {
    if (restoringHistory) { restoringHistory = false; event.stopImmediatePropagation(); return }
    const nextIndex = event.state?.idx
    if (navigationDecision && typeof navigationIndex === 'number' && typeof nextIndex === 'number' && nextIndex !== navigationIndex && !navigationDecision()) {
      event.stopImmediatePropagation()
      restoringHistory = true
      window.history.go(navigationIndex - nextIndex)
    }
  }, true)
}
