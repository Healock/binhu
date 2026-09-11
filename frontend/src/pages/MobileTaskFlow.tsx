import { useEffect, useRef } from 'react'
import { Routes, Route, useLocation } from 'react-router-dom'
import MobileTaskList from './MobileTaskList'
import MobileTaskDetail from './MobileTaskDetail'
import { clearMobileTaskListRestoration, clearMobileTaskListSnapshot } from '../utils/mobileTaskListState'

/** Keep only this module's last list alive. Detail routes retain their own params. */
export default function MobileTaskFlow() {
  const location = useLocation()
  const active = /^\/tasks\/?$/.test(location.pathname)
  const listLocation = useRef<typeof location | null>(null)
  if (active) listLocation.current = location
  useEffect(() => () => {
    clearMobileTaskListSnapshot()
    clearMobileTaskListRestoration(window.sessionStorage)
  }, [])
  return <>
    {listLocation.current && <div hidden={!active} data-task-list-retained>
      <Routes location={listLocation.current}>
        <Route index element={<MobileTaskList active={active} />} />
      </Routes>
    </div>}
    <Routes>
      <Route index element={null} />
      <Route path=":parserType/:rowKey" element={<MobileTaskDetail />} />
    </Routes>
  </>
}
