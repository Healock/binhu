import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import 'antd/dist/reset.css'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import App from './App'
import './index.css'

dayjs.locale('zh-cn')

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#1d4ed8',
          colorSuccess: '#15803d',
          colorWarning: '#c2410c',
          colorError: '#b91c1c',
          colorText: '#172033',
          colorTextSecondary: '#64748b',
          colorBorder: '#dbe2ea',
          colorBgLayout: '#f4f6f9',
          borderRadius: 8,
          controlHeight: 36,
          fontSize: 14,
          boxShadowSecondary: '0 8px 24px rgba(15, 23, 42, 0.10)',
        },
        components: {
          Button: {
            fontWeight: 500,
            primaryShadow: 'none',
          },
          Table: {
            headerBg: '#f8fafc',
            headerColor: '#475569',
            borderColor: '#e2e8f0',
            rowHoverBg: '#f8fbff',
          },
          Menu: {
            itemBorderRadius: 8,
            itemSelectedBg: '#eff6ff',
            itemSelectedColor: '#1d4ed8',
          },
        },
      }}
    >
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ConfigProvider>
  </React.StrictMode>,
)
