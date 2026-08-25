import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import 'antd/dist/reset.css'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import App from './App'
import {
  applyThemeToDocument,
  readStoredThemeMode,
  resolveThemeMode,
  THEME_MEDIA_QUERY,
} from './utils/themeMode'
import './index.css'

dayjs.locale('zh-cn')

if (import.meta.env.VITE_NATIVE_MOBILE === 'true') {
  document.documentElement.classList.add('native-mobile-shell')

  // Keep tablets in the mobile CSS range while exposing display cutout insets
  // on ordinary phones. The Android host also applies native system-bar insets.
  document.querySelector<HTMLMetaElement>('meta[name="viewport"]')?.setAttribute(
    'content',
    window.innerWidth >= 768
      ? 'width=600, initial-scale=1.0, viewport-fit=cover'
      : 'width=device-width, initial-scale=1.0, viewport-fit=cover',
  )
}

if (import.meta.env.VITE_DESKTOP_MODE === 'true') {
  document.documentElement.classList.add('desktop-shell')
}

if (
  import.meta.env.VITE_DESKTOP_MODE === 'true'
  && (window.location.pathname === '/' || window.location.pathname.endsWith('/index.html'))
) {
  window.history.replaceState({}, '', '/login')
}

applyThemeToDocument(resolveThemeMode(
  readStoredThemeMode(window.localStorage),
  window.matchMedia(THEME_MEDIA_QUERY).matches,
))

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
