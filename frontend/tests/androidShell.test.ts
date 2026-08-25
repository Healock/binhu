import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('Android client locks portrait and forces the packaged frontend into mobile mode', () => {
  const androidEnvironment = readFileSync(new URL('../.env.android', import.meta.url), 'utf8')
  const mainSource = readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8')
  const viewportHook = readFileSync(new URL('../src/hooks/useMobileViewport.ts', import.meta.url), 'utf8')
  const polyfills = readFileSync(new URL('../public/legacy-polyfills.js', import.meta.url), 'utf8')
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
  const nativePhone = readFileSync(new URL('../src/utils/nativePhone.ts', import.meta.url), 'utf8')
  const tauriLibrary = readFileSync(
    new URL('../../mobile/apps/android-tauri/src-tauri/src/lib.rs', import.meta.url),
    'utf8',
  )
  const capability = readFileSync(
    new URL('../../mobile/apps/android-tauri/src-tauri/capabilities/android-main.json', import.meta.url),
    'utf8',
  )
  const manifest = readFileSync(
    new URL('../../mobile/apps/android-tauri/src-tauri/gen/android/app/src/main/AndroidManifest.xml', import.meta.url),
    'utf8',
  )
  const activity = readFileSync(
    new URL('../../mobile/apps/android-tauri/src-tauri/gen/android/app/src/main/java/com/bhzh/binhu/android/MainActivity.kt', import.meta.url),
    'utf8',
  )
  const layout = readFileSync(new URL('../src/components/Layout.tsx', import.meta.url), 'utf8')
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')
  const updateGate = readFileSync(new URL('../src/components/MandatoryUpdateGate.tsx', import.meta.url), 'utf8')
  const updateCoordinator = readFileSync(new URL('../src/components/ClientUpdateCoordinator.tsx', import.meta.url), 'utf8')

  assert.match(androidEnvironment, /^VITE_NATIVE_MOBILE=true$/m)
  assert.match(mainSource, /classList\.add\('native-mobile-shell'\)/)
  assert.match(mainSource, /width=600, initial-scale=1\.0, viewport-fit=cover/)
  assert.match(mainSource, /width=device-width, initial-scale=1\.0, viewport-fit=cover/)
  assert.match(viewportHook, /NATIVE_MOBILE\s*\|\|/)
  assert.match(polyfills, /Object\.prototype\.hasOwnProperty\.call/)
  assert.ok(html.indexOf('/legacy-polyfills.js') < html.indexOf('/src/main.tsx'))
  assert.match(nativePhone, /openUrl\(`tel:\$\{normalized\}`\)/)
  assert.match(tauriLibrary, /tauri_plugin_opener::init/)
  assert.match(capability, /"opener:default"/)
  assert.match(manifest, /android:screenOrientation="portrait"/)
  assert.match(manifest, /android:resizeableActivity="false"/)
  assert.doesNotMatch(manifest, /LEANBACK_LAUNCHER/)
  assert.match(activity, /SCREEN_ORIENTATION_PORTRAIT/)
  assert.match(activity, /SystemBarStyle\.light\(Color\.WHITE, Color\.WHITE\)/)
  assert.match(activity, /findViewById<View>\(android\.R\.id\.content\)/)
  assert.match(activity, /WindowInsetsCompat\.Type\.displayCutout/)
  assert.match(activity, /view\.setPadding\(safeArea\.left, safeArea\.top, safeArea\.right, safeArea\.bottom\)/)
  assert.doesNotMatch(activity, /setOnApplyWindowInsetsListener\(webView\)/)
  assert.match(activity, /useWideViewPort = false/)
  assert.match(layout, /mobile-account-trigger ml-auto flex shrink-0/)
  assert.doesNotMatch(layout, /aria-label="打开账号菜单"[\s\S]{0,160}mr-16/)
  assert.match(layout, /mobile-app-header md:hidden fixed/)
  assert.match(layout, /mainRef\.current\?\.scrollTo\(\{ top: 0, left: 0, behavior: 'auto' \}\)/)
  assert.match(layout, /window\.scrollTo\(\{ top: 0, left: 0, behavior: 'auto' \}\)/)
  assert.match(styles, /\.online-presence-indicator\s*\{[\s\S]*?right:\s*64px;/)
  assert.match(styles, /html\.native-mobile-shell \.app-shell\s*\{[\s\S]*?flex-direction:\s*column;/)
  assert.match(styles, /html\.native-mobile-shell \.mobile-app-header\s*\{[\s\S]*?position:\s*sticky;/)
  assert.match(styles, /html\.native-mobile-shell \.app-shell > main\s*\{[^}]*overflow:\s*visible;/)
  assert.doesNotMatch(styles, /html\.native-mobile-shell \.app-shell > main\s*\{[^}]*overflow-y:\s*auto;/)
  assert.match(styles, /html\.native-mobile-shell \.app-content\s*\{[\s\S]*?padding-top:\s*16px !important;/)
  assert.match(updateGate, /!android &&.*进入离线模式/)
  assert.match(updateGate, /isAndroidClientRuntime\(\) && !status/)
  assert.match(updateCoordinator, /INITIAL_CHECK_DELAY_MS = 15_000/)
  assert.match(updateCoordinator, /CHECK_INTERVAL_MS = 6 \* 60 \* 60 \* 1000/)
  assert.match(updateCoordinator, /visibilitychange/)
})
