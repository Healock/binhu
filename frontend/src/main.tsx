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
