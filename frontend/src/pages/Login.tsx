import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Alert, Button, Input } from 'antd'
import {
  LockOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useAuth } from '../context/AuthContext'
import loginBlueGrid from '../assets/login/login-blue-grid.png'
import loginSilkCity from '../assets/login/login-silk-city.png'
import policeEmblem from '../assets/login/police-emblem.png'

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
    <div className="login-page">
      <section
        className="login-brand-panel"
        style={{ backgroundImage: `url(${loginBlueGrid})` }}
        aria-label="数智赋能，守护平安滨湖"
      >
        <img className="login-brand-panel__city" src={loginSilkCity} alt="" aria-hidden="true" />

        <div className="login-brand-panel__agency">
          <span className="login-brand-panel__agency-mark">吴</span>
          <span>吴江公安 · 智慧警务</span>
        </div>

        <div className="login-brand-panel__message">
          <div className="login-brand-panel__location">SUZHOU · WUJIANG</div>
          <h1>
            <span>数智赋能</span>
            <span>守护平安滨湖</span>
          </h1>
        </div>

        <div className="login-brand-panel__values">智慧 <i /> 协同 <i /> 高效</div>
      </section>

      <main className="login-form-panel">
        <div className="login-form-shell">
          <header className="login-product-brand">
            <img src={policeEmblem} alt="中华人民共和国人民警察警徽" />
            <h2>滨湖公安智慧平台</h2>
            <p>BINHU PUBLIC SECURITY SMART PLATFORM</p>
          </header>

          <div className="login-form-card">
            <div className="login-form-card__heading">
              <h3>登录系统</h3>
              <p>请使用平台账号进入系统</p>
            </div>

            <form onSubmit={handleSubmit} className="login-form">
              <label className="login-form__field">
                <span>用户名</span>
                <Input
                  size="large"
                  prefix={<UserOutlined />}
                  value={username}
                  onChange={event => setUsername(event.target.value)}
                  placeholder="请输入用户名"
                  autoFocus
                  autoComplete="username"
                  aria-label="用户名"
                />
              </label>

              <label className="login-form__field">
                <span>密码</span>
                <Input.Password
                  size="large"
                  prefix={<LockOutlined />}
                  value={password}
                  onChange={event => setPassword(event.target.value)}
                  placeholder="请输入密码"
                  autoComplete="current-password"
                  aria-label="密码"
                />
              </label>

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
