import { clearRoleDashboardCaches } from "./dashboardCache.ts";
export const ATTENDANCE_CHANGED = "binhu:attendance-changed";
const SIGNAL = "binhu:attendance:changed";
export function notifyAttendanceChanged() {
  try {
    clearRoleDashboardCaches(window.sessionStorage);
  } catch {
    /* Storage is optional. */
  }
  window.dispatchEvent(new Event(ATTENDANCE_CHANGED));
  try {
    window.localStorage.setItem(SIGNAL, crypto.randomUUID());
  } catch {
    /* Other tabs refresh on focus. */
  }
}
export function watchAttendanceChanges(reload: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key === SIGNAL) {
      try {
        clearRoleDashboardCaches(window.sessionStorage);
      } catch {
        /* Storage is optional. */
      }
      reload();
    }
  };
  window.addEventListener(ATTENDANCE_CHANGED, reload);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(ATTENDANCE_CHANGED, reload);
    window.removeEventListener("storage", onStorage);
  };
}
