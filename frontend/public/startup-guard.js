(function installStartupGuard() {
  var failureShown = false

  function webViewVersion() {
    var match = String(window.navigator && window.navigator.userAgent || '').match(/(?:Chrome|CriOS)\/(\d+(?:\.\d+)*)/)
    return match ? match[1] : '无法识别'
  }

  function runtimeLabel() {
    var userAgent = String(window.navigator && window.navigator.userAgent || '')
    return userAgent.indexOf('Android') !== -1 ? 'Android System WebView/Chrome' : '浏览器内核'
  }

  function showFailure(message) {
    if (failureShown) return
    var startup = document.getElementById('binhu-startup')
    if (!startup) return
    failureShown = true

    var messageElement = document.getElementById('binhu-startup-message')
    var detailElement = document.getElementById('binhu-startup-detail')
    var retryButton = document.getElementById('binhu-startup-retry')
    var spinner = startup.querySelector('.binhu-startup__spinner')

    if (spinner) spinner.style.display = 'none'
    if (messageElement) messageElement.textContent = '应用未能正常启动'
    if (detailElement) {
      detailElement.hidden = false
      detailElement.textContent = message + ' 当前' + runtimeLabel() + '版本：' + webViewVersion() + '。请联系技术人员检查客户端运行环境。'
    }
    if (retryButton) {
      retryButton.hidden = false
      retryButton.addEventListener('click', function reloadApplication() {
        window.location.reload()
      })
    }
  }

  window.addEventListener('error', function handleStartupError(event) {
    var source = event && event.filename ? String(event.filename) : ''
    if (source.indexOf('/assets/') !== -1 || source.indexOf('tauri.localhost') !== -1) {
      showFailure('本地界面组件与当前系统 WebView 不兼容。')
    }
  })

  window.addEventListener('unhandledrejection', function handleStartupRejection() {
    showFailure('本地界面组件启动时发生异常。')
  })

  window.setTimeout(function detectMissingApplication() {
    if (document.getElementById('binhu-startup')) {
      showFailure('本地界面组件没有完成加载。')
    }
  }, 10000)
})()
