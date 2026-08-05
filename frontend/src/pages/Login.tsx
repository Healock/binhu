import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Alert, Button, Input } from 'antd'
import {
  LockOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useAuth } from '../context/AuthContext'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    const raw = sessionStorage.getItem('auth_exit_reason')
    if (!raw) return
    sessionStorage.removeItem('auth_exit_reason')
    try {
      const reason = JSON.parse(raw)
      setError(reason.message || '登录状态已失效，请重新登录')
    } catch {
      setError('登录状态已失效，请重新登录')
    }
  }, [])

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!username.trim() || !password) return
    setLoading(true)
    setError('')
    try {
      await login(username.trim(), password)
      navigate('/', { replace: true })
    } catch (err: any) {
      setError(err.message || '登录失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page flex min-h-screen">
      <section className="hidden w-[44%] flex-col bg-[#17335c] p-10 text-white md:flex lg:p-14">
        <div className="flex items-center gap-3">
          <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-white text-lg font-semibold text-[#17335c]">
            滨
          </span>
          <div>
            <div className="text-lg font-semibold">滨湖智慧平台</div>
            <div className="text-xs text-blue-100/75">数据管理中心</div>
          </div>
        </div>
      </section>

      <main className="flex flex-1 items-center justify-center p-5 sm:p-8">
        <div className="w-full max-w-[400px]">
          <div className="mb-7 md:hidden">
            <div className="mb-3 flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-blue-700 font-semibold text-white">滨</span>
              <span className="text-lg font-semibold text-slate-900">滨湖智慧平台</span>
            </div>
            <p className="text-sm text-slate-500">数据管理中心</p>
          </div>

          <div className="app-card app-card--padded">
            <div className="mb-6">
              <h2 className="text-xl font-semibold text-slate-900">登录系统</h2>
            </div>

            <form onSubmit={handleSubmit} className="grid gap-5">
              <div className="grid gap-2">
                <label className="mb-1.5 block text-sm font-medium text-slate-700">用户名</label>
                <Input
                  size="large"
                  prefix={<UserOutlined className="text-slate-400" />}
                  value={username}
                  onChange={event => setUsername(event.target.value)}
                  placeholder="请输入用户名"
                  autoFocus
                  autoComplete="username"
                />
              </div>
              <div className="grid gap-2">
                <label className="mb-1.5 block text-sm font-medium text-slate-700">密码</label>
                <Input.Password
                  size="large"
                  prefix={<LockOutlined className="text-slate-400" />}
                  value={password}
                  onChange={event => setPassword(event.target.value)}
                  placeholder="请输入密码"
                  autoComplete="current-password"
                />
              </div>
              <div className="grid gap-3 pt-1">
                {error && <Alert type="error" showIcon message={error} />}
                <Button
                  type="primary"
                  htmlType="submit"
                  size="large"
                  loading={loading}
                  disabled={!username.trim() || !password}
                  block
                >
                  登录
                </Button>
              </div>
            </form>
          </div>
        </div>
      </main>
    </div>
  )
}
